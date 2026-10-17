"""FalkorDB graph model and MCP tools for Wry Change Impact."""

import os
import secrets
import time
import uuid
import ast
import concurrent.futures
import hashlib
import warnings
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

try:
    # The mcp package's own FastMCP Settings model declares a `lifespan`
    # field whose forward reference pydantic-settings cannot always resolve,
    # emitting IncompleteFieldDefinitionWarning when the server is created.
    # Wry never configures a lifespan, so the warning is noise: ignore just
    # this warning category rather than touching third-party code.
    from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning
    warnings.filterwarnings("ignore", category=IncompleteFieldDefinitionWarning)
except ImportError:  # pragma: no cover - pydantic-settings is an mcp dependency
    pass


REPOSITORY = "northstar-commerce-demo"
FILES = [
    ("src/api/auth_routes.py", "module", "Authentication HTTP routes", "high"),
    ("src/core/auth_service.py", "module", "Token and session orchestration", "critical"),
    ("src/core/token_store.py", "module", "Token persistence adapter", "high"),
    ("src/core/session.py", "module", "Session lifecycle and expiry", "high"),
    ("src/users/profile.py", "module", "User profile read/write service", "medium"),
    ("src/checkout/checkout.py", "module", "Checkout orchestration", "high"),
    ("src/checkout/payment_gateway.py", "module", "Payment provider adapter", "critical"),
    ("src/notifications/email.py", "module", "Transactional email sender", "low"),
    ("tests/test_auth_service.py", "test", "Auth service unit tests", "high"),
    ("tests/test_auth_routes.py", "test", "Authentication route tests", "high"),
    ("tests/test_token_store.py", "test", "Token store contract tests", "high"),
    ("tests/test_session.py", "test", "Session lifecycle tests", "medium"),
    ("tests/test_profile.py", "test", "Profile service tests", "medium"),
    ("tests/test_checkout.py", "test", "Checkout integration tests", "high"),
    ("tests/test_payment_gateway.py", "test", "Payment adapter contract tests", "high"),
]

# IMPORTS point from importer to dependency; COVERS points from test to code.
IMPORTS = [
    ("src/api/auth_routes.py", "src/core/auth_service.py"),
    ("src/api/auth_routes.py", "src/core/session.py"),
    ("src/core/auth_service.py", "src/core/token_store.py"),
    ("src/core/auth_service.py", "src/core/session.py"),
    ("src/core/auth_service.py", "src/notifications/email.py"),
    ("src/users/profile.py", "src/core/auth_service.py"),
    ("src/checkout/checkout.py", "src/core/auth_service.py"),
    ("src/checkout/checkout.py", "src/checkout/payment_gateway.py"),
]
COVERS = [
    ("tests/test_auth_service.py", "src/core/auth_service.py"),
    ("tests/test_auth_service.py", "src/core/token_store.py"),
    ("tests/test_auth_routes.py", "src/api/auth_routes.py"),
    ("tests/test_auth_routes.py", "src/core/auth_service.py"),
    ("tests/test_token_store.py", "src/core/token_store.py"),
    ("tests/test_session.py", "src/core/session.py"),
    ("tests/test_profile.py", "src/users/profile.py"),
    ("tests/test_checkout.py", "src/checkout/checkout.py"),
    ("tests/test_checkout.py", "src/core/auth_service.py"),
    ("tests/test_checkout.py", "src/checkout/payment_gateway.py"),
    ("tests/test_payment_gateway.py", "src/checkout/payment_gateway.py"),
]

_ACTIVE_REPOSITORY_ID = REPOSITORY
_ACTIVE_REPOSITORY_NAME = "Northstar Commerce"
_ACTIVE_DATASET = "demo"
_IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}
_MAX_SOURCE_FILES = 2000
_MAX_FILE_BYTES = 1_000_000


# Core Cypher queries. Kept as constants so the exact executed queries can be
# shown in the UI/demo as provenance, and so tests can assert on them.
IMPACT_TRAVERSAL_CYPHER = (
    "MATCH (:Repository {id:$repo})-[:CONTAINS]->(consumer:File {repository_id:$repo, kind:'module'}), "
    "(:Repository {id:$repo})-[:CONTAINS]->(target:File {repository_id:$repo, path:$target}), "
    "p=(consumer)-[:IMPORTS*1..5]->(target) "
    "RETURN consumer.path, consumer.criticality, length(p), [n IN nodes(p) | n.path] "
    "ORDER BY length(p), consumer.path"
)
TARGET_CRITICALITY_CYPHER = (
    "MATCH (:Repository {id:$repo})-[:CONTAINS]->(f:File {repository_id:$repo, path:$target, kind:'module'}) "
    "RETURN f.criticality"
)
COVERAGE_CYPHER = (
    "MATCH (:Repository {id:$repo})-[:CONTAINS]->(test:File {repository_id:$repo, kind:'test'}) "
    "-[:COVERS]->(covered:File {repository_id:$repo}) "
    "WHERE covered.path IN $paths RETURN test.path, collect(DISTINCT covered.path) ORDER BY test.path"
)
MODULE_COUNT_CYPHER = (
    "MATCH (:Repository {id:$repo})-[:CONTAINS]->(f:File {repository_id:$repo, kind:'module'}) "
    "RETURN count(f)"
)
# Named graph algorithm: PageRank over the import graph. Scores are based on
# incoming IMPORTS edges, so a module scores highly when much of the repository
# depends on it — a direct "blast radius" signal for ranking tests.
# Scores are computed over File/IMPORTS graph-wide, then filtered to this
# repository; with several indexed repositories the scores are relative to the
# whole graph, which is fine for the single-repo demo and documented here.
PAGERANK_CYPHER = (
    "CALL algo.pageRank('File', 'IMPORTS') YIELD node, score "
    "WHERE node.repository_id = $repo "
    "RETURN node.path AS path, score ORDER BY score DESC"
)
# Named graph algorithm: betweenness centrality over the import graph. A module
# scores highly when it lies on many dependency paths — the architectural
# choke points, a different signal from PageRank's popularity.
BETWEENNESS_CYPHER = (
    "CALL algo.betweenness({nodeLabels: ['File'], relationshipTypes: ['IMPORTS']}) "
    "YIELD node, score "
    "WHERE node.repository_id = $repo AND node.kind = 'module' "
    "RETURN node.path AS path, score ORDER BY score DESC LIMIT 10"
)
# Named graph algorithm: community detection via label propagation (CDLP) over
# the import graph. Densely import-connected modules converge on a shared
# community id — the architectural neighborhoods of the codebase. A change's
# "blast radius" is the set of communities its transitive impact touches.
# IMPORTS edges are module-to-module only in both demo and local modes (tests
# connect via COVERS), so the procedure never sees test nodes; the WHERE clause
# below is a final guard, not the thing that excludes them.
# Note: CDLP processes nodes in random order, so community *ids* are not
# stable across runs; the grouping structure is what the agent reports.
COMMUNITY_CYPHER = (
    "CALL algo.labelPropagation({nodeLabels: ['File'], relationshipTypes: ['IMPORTS']}) "
    "YIELD node, communityId "
    "WHERE node.repository_id = $repo AND node.kind = 'module' "
    "RETURN node.path AS path, communityId AS community ORDER BY community, path"
)
# Shortest directed dependency path between two modules (importer -> dependency).
SHORTEST_PATH_CYPHER = (
    "MATCH (a:File {repository_id:$repo, path:$from}), (b:File {repository_id:$repo, path:$to}) "
    "WITH a, b, shortestPath((a)-[:IMPORTS*1..10]->(b)) AS p "
    "WHERE p IS NOT NULL "
    "RETURN [n IN nodes(p) | n.path] AS chain, length(p) AS hops"
)
# Agent memory: past investigations, newest first.
RECALL_CYPHER = (
    "MATCH (i:Investigation)-[:FOR_TARGET]->(f:File {repository_id:$repo, path:$target}) "
    "RETURN i.created_at AS created_at, i.tool AS tool, i.importer_count AS importers, "
    "i.test_count AS tests, i.top_test AS top_test, i.summary AS summary "
    "ORDER BY i.created_at DESC LIMIT 5"
)


def _timed_ro_query(graph, query, params=None):
    """Run a read-only query and return (result_set, elapsed_ms)."""
    start = time.perf_counter()
    result_set = graph.ro_query(query, params or {}).result_set
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    return result_set, elapsed_ms


def _require_module_target(graph, target_path: str):
    """Return the target's criticality or raise a helpful error for unknown targets."""
    target_rows = graph.ro_query(
        TARGET_CRITICALITY_CYPHER,
        {"repo": _ACTIVE_REPOSITORY_ID, "target": target_path},
    ).result_set
    if not target_rows:
        raise ValueError(f"'{target_path}' is not in the indexed repository.")
    return target_rows[0][0]


def active_repository_info():
    return {
        "id": _ACTIVE_REPOSITORY_ID,
        "name": _ACTIVE_REPOSITORY_NAME,
        "dataset": _ACTIVE_DATASET,
    }


def _local_repository_root():
    configured = os.getenv("WRY_REPOSITORY_PATH")
    return Path(configured).expanduser().resolve() if configured else Path(__file__).parent.parent.resolve()


def local_repository_name():
    return _local_repository_root().name


def repository_index_enabled():
    return os.getenv("WRY_ALLOW_REPOSITORY_METADATA_UPLOAD", "false").lower() in {"1", "true", "yes"}


def _module_name(relative_path: Path):
    parts = list(relative_path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _local_imports(path: Path, tree: ast.Module, module_paths: dict[str, str]):
    imports = set()
    package = list(path.parent.parts)
    for node in ast.walk(tree):
        candidates = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module.split(".") if node.module else []
            if node.level:
                prefix = package[:max(0, len(package) - node.level + 1)]
                base = prefix + base
            if base:
                candidates.append(".".join(base))
            for alias in node.names:
                if alias.name != "*":
                    candidates.append(".".join(base + [alias.name]))
        for name in candidates:
            if name in module_paths and module_paths[name] != path.as_posix():
                imports.add(module_paths[name])
    return sorted(imports)


def scan_local_repository(root: Path):
    if not root.is_dir():
        raise ValueError("The configured repository path is not a directory.")

    source_paths = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in _IGNORED_DIRS and not name.startswith("."))
        for filename in filenames:
            path = Path(directory) / filename
            if path.suffix == ".py" and not path.is_symlink() and path.stat().st_size <= _MAX_FILE_BYTES:
                source_paths.append(path)
                if len(source_paths) > _MAX_SOURCE_FILES:
                    raise ValueError(f"Repository exceeds the {_MAX_SOURCE_FILES}-file indexing limit.")

    source_paths.sort()
    relative_paths = {path: path.relative_to(root).as_posix() for path in source_paths}
    test_relatives = {
        relative
        for path, relative in relative_paths.items()
        if path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or "tests" in path.relative_to(root).parts
    }
    module_paths = {}
    for path, relative in relative_paths.items():
        module_paths[_module_name(Path(relative))] = relative
    basename_candidates = {}
    for relative in relative_paths.values():
        basename_candidates.setdefault(Path(relative).stem, []).append(relative)
    module_paths.update({name: paths[0] for name, paths in basename_candidates.items() if len(paths) == 1})

    files = []
    import_edges = set()
    test_edges = set()
    for path in source_paths:
        relative = relative_paths[path]
        is_test = relative in test_relatives
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, UnicodeError, SyntaxError):
            tree = ast.Module(body=[], type_ignores=[])
        dependencies = _local_imports(Path(relative), tree, module_paths)
        criticality = "low" if is_test else "high" if any(word in relative.lower() for word in ("auth", "security", "credential", "payment")) else "medium"
        files.append({
            "path": relative,
            "kind": "test" if is_test else "module",
            "summary": "Python test module" if is_test else "Python source module",
            "criticality": criticality,
        })
        for dependency in dependencies:
            # IMPORTS edges are module-to-module only, matching the demo data
            # model. Test files connect to modules via COVERS edges instead, so
            # test nodes never shape module-level graph algorithms (PageRank,
            # betweenness, CDLP communities) that run over IMPORTS. This holds
            # in both directions: test files never emit IMPORTS edges, and a
            # module importing a test helper emits no IMPORTS edge to the test
            # file (the data model has no relationship for that direction).
            if is_test:
                test_edges.add((relative, dependency))
            elif dependency in test_relatives:
                continue
            else:
                import_edges.add((relative, dependency))

    if not any(row["kind"] == "module" for row in files):
        raise ValueError("No Python source modules were found in the configured repository.")
    return files, sorted(import_edges), sorted(test_edges)


def index_local_repository():
    global _ACTIVE_REPOSITORY_ID, _ACTIVE_REPOSITORY_NAME, _ACTIVE_DATASET
    if not repository_index_enabled():
        raise ValueError(
            "Repository indexing is disabled. Enable it only for a public or otherwise non-restricted repository."
        )
    root = _local_repository_root()
    files, imports, covers = scan_local_repository(root)
    repo_id = "local-" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    graph = get_graph()
    graph.query(
        "MERGE (r:Repository {id:$repo}) SET r.name=$name, r.synthetic=false, "
        "r.provenance='local Python import metadata'",
        {"repo": repo_id, "name": root.name},
    )
    graph.query(
        "MATCH (r:Repository {id:$repo})-[:CONTAINS]->(f:File {repository_id:$repo}) "
        "WHERE NOT f.path IN $paths DETACH DELETE f",
        {"repo": repo_id, "paths": [row["path"] for row in files]},
    )
    graph.query(
        "MATCH ()-[edge:IMPORTS {repository_id:$repo}]->() DELETE edge",
        {"repo": repo_id},
    )
    graph.query(
        "MATCH ()-[edge:COVERS {repository_id:$repo}]->() DELETE edge",
        {"repo": repo_id},
    )
    graph.query(
        "MATCH (r:Repository {id:$repo}) UNWIND $files AS row "
        "MERGE (f:File {repository_id:$repo, path:row.path}) SET f.kind=row.kind, f.summary=row.summary, "
        "f.criticality=row.criticality, f.provenance='local Python import metadata' "
        "MERGE (r)-[:CONTAINS {repository_id:$repo}]->(f)",
        {"repo": repo_id, "files": files},
    )
    graph.query(
        "UNWIND $edges AS edge MATCH (a:File {repository_id:$repo, path:edge.importer}), "
        "(b:File {repository_id:$repo, path:edge.dependency}) "
        "MERGE (a)-[:IMPORTS {repository_id:$repo, provenance:'local Python import'}]->(b)",
        {"repo": repo_id, "edges": [{"importer": a, "dependency": b} for a, b in imports]},
    ) if imports else None
    graph.query(
        "UNWIND $edges AS edge MATCH (t:File {repository_id:$repo, path:edge.test}), "
        "(f:File {repository_id:$repo, path:edge.covered}) "
        "MERGE (t)-[:COVERS {repository_id:$repo, provenance:'test imports local module'}]->(f)",
        {"repo": repo_id, "edges": [{"test": a, "covered": b} for a, b in covers]},
    ) if covers else None
    _ACTIVE_REPOSITORY_ID = repo_id
    _ACTIVE_REPOSITORY_NAME = root.name
    _ACTIVE_DATASET = "local"
    return {
        "repository": root.name,
        "source_files": sum(row["kind"] == "module" for row in files),
        "test_files": sum(row["kind"] == "test" for row in files),
        "import_edges": len(imports),
        "coverage_edges": len(covers),
        "metadata_only": True,
    }


def _settings():
    url = os.getenv("FALKORDB_URL")
    if url:
        parsed = urlparse(url)
        return {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 6379,
            "username": parsed.username,
            "password": parsed.password,
            "ssl": parsed.scheme == "rediss",
        }
    return {
        "host": os.getenv("FALKORDB_HOST", "localhost"),
        "port": int(os.getenv("FALKORDB_PORT", "6379")),
        "username": os.getenv("FALKORDB_USERNAME"),
        "password": os.getenv("FALKORDB_PASSWORD"),
        "ssl": os.getenv("FALKORDB_SSL", "false").lower() in {"1", "true", "yes"},
    }


def graph_status():
    settings = _settings()
    configured = bool(settings["password"] or os.getenv("FALKORDB_ALLOW_NO_AUTH") == "true")
    return {
        "configured": configured,
        "provider": "FalkorDB",
        "graph": os.getenv("FALKORDB_GRAPH", "wry_change_impact"),
        "host": settings["host"] if configured else None,
    }


def get_graph():
    settings = _settings()
    if not (settings["password"] or os.getenv("FALKORDB_ALLOW_NO_AUTH") == "true"):
        raise RuntimeError("FalkorDB is not configured. Add the instance connection settings to .env.")
    from falkordb import FalkorDB

    return FalkorDB(**settings).select_graph(os.getenv("FALKORDB_GRAPH", "wry_change_impact"))


def seed_demo(graph):
    graph.query(
        "MERGE (r:Repository {id:$repo}) SET r.name='Northstar Commerce', r.synthetic=true, "
        "r.description='Illustrative demo dependency graph; not a real repository'",
        {"repo": REPOSITORY},
    )
    graph.query(
        "MATCH (r:Repository {id:$repo})-[:CONTAINS]->(f:File) "
        "WHERE f.repository_id IS NULL SET f.repository_id=$repo",
        {"repo": REPOSITORY},
    )
    graph.query(
        "MATCH (r:Repository {id:$repo}) UNWIND $files AS row "
        "MERGE (f:File {repository_id:$repo, path:row.path}) SET f.kind=row.kind, f.summary=row.summary, "
        "f.criticality=row.criticality, f.provenance='synthetic demo fixture' "
        "MERGE (r)-[:CONTAINS]->(f)",
        {"repo": REPOSITORY, "files": [
            {"path": path, "kind": kind, "summary": summary, "criticality": criticality}
            for path, kind, summary, criticality in FILES
        ]},
    )
    graph.query(
        "UNWIND $edges AS edge MATCH (a:File {repository_id:$repo, path:edge.importer}), "
        "(b:File {repository_id:$repo, path:edge.dependency}) "
        "MERGE (a)-[:IMPORTS {repository_id:$repo, provenance:'synthetic demo dependency'}]->(b)",
        {"repo": REPOSITORY, "edges": [{"importer": importer, "dependency": dependency} for importer, dependency in IMPORTS]},
    )
    graph.query(
        "UNWIND $edges AS edge MATCH (t:File {repository_id:$repo, path:edge.test}), "
        "(f:File {repository_id:$repo, path:edge.covered}) "
        "MERGE (t)-[:COVERS {repository_id:$repo, provenance:'synthetic demo coverage mapping'}]->(f)",
        {"repo": REPOSITORY, "edges": [{"test": test, "covered": covered} for test, covered in COVERS]},
    )


def list_targets():
    graph = get_graph()
    rows = graph.ro_query(
        "MATCH (:Repository {id:$repo})-[:CONTAINS]->(f:File {repository_id:$repo}) "
        "WHERE f.kind='module' RETURN f.path, f.summary, f.criticality ORDER BY f.path",
        {"repo": _ACTIVE_REPOSITORY_ID},
    ).result_set
    return [{"path": row[0], "summary": row[1], "criticality": row[2]} for row in rows]


def trace_change_impact(target_path: str):
    graph = get_graph()
    rows, impact_ms = _timed_ro_query(
        graph, IMPACT_TRAVERSAL_CYPHER,
        {"repo": _ACTIVE_REPOSITORY_ID, "target": target_path},
    )
    criticality = _require_module_target(graph, target_path)

    impacted = [{"path": target_path, "distance": 0, "criticality": criticality, "path_nodes": [target_path]}]
    seen = {target_path}
    for consumer_path, criticality, distance, path_nodes in rows:
        if consumer_path in seen:
            continue
        seen.add(consumer_path)
        impacted.append({
            "path": consumer_path,
            "distance": int(distance),
            "criticality": criticality,
            "path_nodes": list(reversed(path_nodes)),
        })
    paths = [item["path"] for item in impacted]
    test_rows, coverage_ms = _timed_ro_query(
        graph, COVERAGE_CYPHER,
        {"repo": _ACTIVE_REPOSITORY_ID, "paths": paths},
    )
    tests = [{"path": row[0], "covers": row[1]} for row in test_rows]
    module_rows, count_ms = _timed_ro_query(
        graph, MODULE_COUNT_CYPHER,
        {"repo": _ACTIVE_REPOSITORY_ID},
    )
    module_count = int(module_rows[0][0]) if module_rows else 0
    surface_pct = round(100 * len(impacted) / module_count) if module_count else 0
    return {
        "repository": _ACTIVE_REPOSITORY_NAME,
        "target": target_path,
        "impact_count": len(impacted) - 1,
        "affected_files": impacted,
        "recommended_tests": tests,
        "change_surface_score": surface_pct,
        "reasoning": f"FalkorDB found {len(impacted) - 1} transitive importers across up to 5 hops. The changed target plus importers touch {len(impacted)} of {module_count} indexed modules; {len(tests)} tests cover that surface.",
        "tool": "trace_change_impact",
        "graph": os.getenv("FALKORDB_GRAPH", "wry_change_impact"),
        "cypher": {
            "impact_traversal": IMPACT_TRAVERSAL_CYPHER,
            "test_coverage": COVERAGE_CYPHER,
        },
        "timings": {
            "impact_traversal_ms": impact_ms,
            "test_coverage_ms": coverage_ms,
            "module_count_ms": count_ms,
        },
        "provenance": "Local Python import and test-import metadata." if _ACTIVE_DATASET == "local" else "Synthetic dependency and coverage edges, seeded idempotently into FalkorDB.",
    }


# Proposal/approval lifecycle for the write tool. The agent only *proposes* a
# test plan; executing the write requires a token the server issued alongside
# that proposal AND that the user explicitly approved via POST /api/agent/test-plan.
# A token fresh from /api/agent/run or /api/agent/investigate has status
# "proposed" — the MCP write tool refuses it. Only the REST test-plan endpoint
# (the user's approval gesture) flips it to "approved", and the tool consumes
# it exactly once. So a client that grabs a proposal token cannot bypass
# approval by calling the MCP tool directly.
class PlanApprovalError(ValueError):
    """Raised when a test-plan write is attempted without a valid approval."""


_PLAN_APPROVALS: dict[str, dict] = {}
_PLAN_APPROVAL_TTL_SECONDS = 15 * 60


def _issue_plan_approval(target_path: str, tests: list[str], test_order: str = "pagerank") -> str:
    """Issue a *proposal* token. Not usable for writing until approved.

    test_order records how the proposed tests were ordered ("pagerank" or
    "trace") so the saved plan can report it truthfully.
    """
    now = time.time()
    # Prune expired tokens so the store cannot grow unboundedly.
    for token in [t for t, rec in _PLAN_APPROVALS.items() if rec["expires_at"] <= now]:
        del _PLAN_APPROVALS[token]
    token = secrets.token_urlsafe(32)
    _PLAN_APPROVALS[token] = {
        "tool": "create_test_plan_tool",
        "target_path": target_path,
        "tests": list(tests),
        "test_order": test_order,
        "status": "proposed",
        "expires_at": now + _PLAN_APPROVAL_TTL_SECONDS,
    }
    return token


def _check_plan_approval(token: str, target_path: str) -> dict:
    record = _PLAN_APPROVALS.get(token)
    if not record:
        raise PlanApprovalError(
            "Plan proposal missing or unknown. Run the agent (or trace impact) "
            "to propose a plan before saving it."
        )
    if record["expires_at"] < time.time():
        raise PlanApprovalError(
            "Plan proposal expired. Run the agent again to propose a fresh plan."
        )
    if record["target_path"] != target_path:
        raise PlanApprovalError("Plan proposal does not match this target module.")
    return record


def _approve_plan_proposal(token: str, target_path: str) -> dict:
    """Approve a proposal: the user's explicit approval gesture (idempotent)."""
    record = _check_plan_approval(token, target_path)
    record["status"] = "approved"
    return record


def _consume_plan_approval(token: str, target_path: str) -> dict:
    """Validate and burn an *approved* token. The write tool calls this exactly once."""
    record = _check_plan_approval(token, target_path)
    if record["status"] != "approved":
        raise PlanApprovalError(
            "Plan proposal has not been approved. Approve it via POST /api/agent/test-plan first."
        )
    del _PLAN_APPROVALS[token]
    return record


def create_test_plan(target_path: str, rationale: str = "Change impact review",
                     approved_tests: list | None = None, test_order: str = "pagerank"):
    """Persist a proposed test plan. When approved_tests is given - the exact
    list the user reviewed and approved - it is saved verbatim instead of
    being recomputed, so the saved plan can never differ from what was shown.
    """
    ranking = rank_tests_by_pagerank(target_path)
    test_paths = list(approved_tests) if approved_tests is not None else [
        item["path"] for item in ranking["ranked_tests"]]
    graph = get_graph()
    plan_id = str(uuid.uuid4())
    graph.query(
        "MATCH (r:Repository {id:$repo}) "
        "CREATE (p:TestPlan {id:$id, target:$target, rationale:$rationale, status:'proposed', "
        "created_at:timestamp(), provenance:'agent-generated from FalkorDB traversal'}) "
        "MERGE (p)-[:FOR_REPOSITORY]->(r) "
        "WITH p UNWIND $review_paths AS path MATCH (f:File {repository_id:$repo, path:path}) MERGE (p)-[:REVIEW]->(f) "
        "WITH p UNWIND $test_paths AS path MATCH (t:File {repository_id:$repo, path:path}) MERGE (p)-[:RUN]->(t) "
        "RETURN p.id",
        {
            "repo": _ACTIVE_REPOSITORY_ID,
            "id": plan_id,
            "target": target_path,
            "rationale": rationale[:500],
            "review_paths": [item["path"] for item in ranking["affected_files"]],
            "test_paths": test_paths,
        },
    )
    return {
        "saved": True,
        "plan_id": plan_id,
        "target": target_path,
        "files_to_review": len(ranking["affected_files"]),
        "tests_to_run": test_paths,
        "test_order": test_order,
        "action": "Saved a proposed test plan to FalkorDB. Tests are ordered by PageRank importance of the impacted modules they cover. No code was changed and no tests were executed.",
    }


EVALUATION_CASES = [
    {
        "target": "src/core/token_store.py",
        "expected_importers": {
            "src/core/auth_service.py",
            "src/api/auth_routes.py",
            "src/users/profile.py",
            "src/checkout/checkout.py",
        },
        "expected_tests": {
            "tests/test_auth_service.py",
            "tests/test_auth_routes.py",
            "tests/test_token_store.py",
            "tests/test_profile.py",
            "tests/test_checkout.py",
        },
    },
    {
        "target": "src/core/session.py",
        "expected_importers": {
            "src/api/auth_routes.py",
            "src/core/auth_service.py",
            "src/users/profile.py",
            "src/checkout/checkout.py",
        },
        "expected_tests": {
            "tests/test_auth_service.py",
            "tests/test_auth_routes.py",
            "tests/test_session.py",
            "tests/test_profile.py",
            "tests/test_checkout.py",
        },
    },
    {
        "target": "src/notifications/email.py",
        "expected_importers": {
            "src/core/auth_service.py",
            "src/api/auth_routes.py",
            "src/users/profile.py",
            "src/checkout/checkout.py",
        },
        "expected_tests": {
            "tests/test_auth_service.py",
            "tests/test_auth_routes.py",
            "tests/test_profile.py",
            "tests/test_checkout.py",
        },
    },
    {
        "target": "src/checkout/payment_gateway.py",
        "expected_importers": {"src/checkout/checkout.py"},
        "expected_tests": {"tests/test_checkout.py", "tests/test_payment_gateway.py"},
    },
    {"target": "src/missing.py", "expect_error": True},
]


def run_evaluation():
    """Score live FalkorDB traversal results against expected outcomes.

    Each case compares the actual transitive importers and recommended tests
    for a target against the expectations derived from the fixture edges.
    One case expects a clean rejection for an unknown target, one case checks
    the PageRank test ranking for the token-store change, and one case checks
    CDLP community detection for the same change. Returns per-case diffs plus
    a pass/total summary for the demo.

    The expected results are hard-coded for the Northstar Commerce demo
    graph. When a local repository is indexed instead, running the checks
    would produce misleading failures — so evaluation is refused with an
    explanatory reason unless the demo dataset is active.
    """
    if _ACTIVE_DATASET != "demo":
        return {
            "repository": _ACTIVE_REPOSITORY_NAME,
            "dataset": _ACTIVE_DATASET,
            "evaluated": False,
            "cases": [],
            "passed": 0,
            "total": len(EVALUATION_CASES) + 2,
            "reason": ("Evaluation is calibrated for the Northstar Commerce demo graph, "
                       "but the active dataset is a locally indexed repository, so the "
                       "expected results do not apply. Re-seed the demo dataset to run "
                       "the evaluation."),
        }
    cases = []
    started_all = time.time()
    # Cases are independent read-only graph queries, each opening its own
    # client, so they run concurrently: wall time is the slowest case,
    # not the sum (sequential runs took ~38s against FalkorDB Cloud).
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(7, len(EVALUATION_CASES) + 2)) as pool:
        trace_futures = [pool.submit(_evaluate_trace_case, case) for case in EVALUATION_CASES]
        pagerank_future = pool.submit(_evaluate_pagerank_ranking)
        communities_future = pool.submit(_evaluate_communities)
        cases = [future.result() for future in trace_futures]
        cases.append(pagerank_future.result())
        cases.append(communities_future.result())
    return {
        "repository": _ACTIVE_REPOSITORY_NAME,
        "dataset": _ACTIVE_DATASET,
        "evaluated": True,
        "cases": cases,
        "passed": sum(1 for case in cases if case["passed"]),
        "total": len(cases),
        "duration_ms": round((time.time() - started_all) * 1000, 1),
    }


def _evaluate_trace_case(case: dict) -> dict:
    """Score one expected-results case; also used by run_evaluation's pool."""
    started = time.time()
    target = case["target"]
    try:
        result = trace_change_impact(target)
    except ValueError as exc:
        return {
            "target": target,
            "expected_error": bool(case.get("expect_error")),
            "error": str(exc),
            "passed": bool(case.get("expect_error")),
            "duration_ms": round((time.time() - started) * 1000, 1),
        }
    actual_importers = {item["path"] for item in result["affected_files"] if item["distance"] > 0}
    actual_tests = {item["path"] for item in result["recommended_tests"]}
    expected_importers = set(case.get("expected_importers", ()))
    expected_tests = set(case.get("expected_tests", ()))
    passed = (
        actual_importers == expected_importers
        and actual_tests == expected_tests
        and not case.get("expect_error")
    )
    return {
        "target": target,
        "expected_importers": sorted(expected_importers),
        "actual_importers": sorted(actual_importers),
        "missing_importers": sorted(expected_importers - actual_importers),
        "unexpected_importers": sorted(actual_importers - expected_importers),
        "expected_tests": sorted(expected_tests),
        "actual_tests": sorted(actual_tests),
        "missing_tests": sorted(expected_tests - actual_tests),
        "unexpected_tests": sorted(actual_tests - expected_tests),
        "change_surface_score": result["change_surface_score"],
        "passed": passed,
        "duration_ms": round((time.time() - started) * 1000, 1),
    }


def _evaluate_pagerank_ranking():
    """Expected-results case for the PageRank ranking tool.

    For the token-store change, the top-ranked test must cover
    src/core/auth_service.py — the highest-PageRank module in the change
    surface — and scores must be in descending order. A graph or procedure
    failure fails the case with the error instead of aborting the report.
    """
    target = "src/core/token_store.py"
    expected_top_module = "src/core/auth_service.py"
    started = time.time()
    try:
        ranked = rank_tests_by_pagerank(target)["ranked_tests"]
    except Exception as exc:
        return {
            "target": target,
            "kind": "pagerank",
            "passed": False,
            "error": f"PageRank ranking failed: {exc}",
            "duration_ms": round((time.time() - started) * 1000, 1),
        }
    scores_descending = all(
        ranked[i]["pagerank_score"] >= ranked[i + 1]["pagerank_score"]
        for i in range(len(ranked) - 1)
    )
    actual_top_module = ranked[0]["top_covered_module"] if ranked else None
    passed = bool(ranked) and actual_top_module == expected_top_module and scores_descending
    return {
        "target": target,
        "kind": "pagerank",
        "expected_top_module": expected_top_module,
        "actual_top_module": actual_top_module,
        "actual_ranking": [
            {"path": item["path"], "score": item["pagerank_score"]} for item in ranked
        ],
        "scores_descending": scores_descending,
        "passed": passed,
        "duration_ms": round((time.time() - started) * 1000, 1),
    }


def _evaluate_communities():
    """Expected-results case for the CDLP community-detection tool.

    For the token-store change, the tool must place the target in a
    community, report at least one community overall, and mark at least one
    community as touched by the impact. Community *ids* are not asserted:
    CDLP processes nodes in random order, so ids are not stable across runs —
    only the grouping structure is checked. A graph or procedure failure
    fails the case with the error instead of aborting the report.
    """
    target = "src/core/token_store.py"
    started = time.time()
    try:
        result = find_impact_communities(target)
    except Exception as exc:
        return {
            "target": target,
            "kind": "communities",
            "passed": False,
            "error": f"Community detection failed: {exc}",
            "duration_ms": round((time.time() - started) * 1000, 1),
        }
    target_placed = result["target_community"] is not None and any(
        community["contains_target"] for community in result["communities"]
    )
    passed = bool(
        target_placed
        and result["community_count"] >= 1
        and result["impacted_communities"] >= 1
    )
    return {
        "target": target,
        "kind": "communities",
        "target_community": result["target_community"],
        "community_count": result["community_count"],
        "impacted_communities": result["impacted_communities"],
        "passed": passed,
        "duration_ms": round((time.time() - started) * 1000, 1),
    }


def rank_tests_by_pagerank(target_path: str):
    """Rank recommended tests by PageRank importance of the impacted modules they cover.

    Runs FalkorDB's algo.pageRank over the import graph, then orders each
    recommended test by the highest PageRank score among the impacted modules
    it covers. A test ranks highly when it covers a module that much of the
    repository depends on.
    """
    analysis = trace_change_impact(target_path)
    graph = get_graph()
    rows, pagerank_ms = _timed_ro_query(graph, PAGERANK_CYPHER, {"repo": _ACTIVE_REPOSITORY_ID})
    scores = {row[0]: float(row[1]) for row in rows}
    impacted = {item["path"] for item in analysis["affected_files"]}
    ranked = []
    for test in analysis["recommended_tests"]:
        covered = [path for path in test["covers"] if path in impacted]
        score = max([scores.get(path, 0.0) for path in covered], default=0.0)
        ranked.append({
            "path": test["path"],
            "covers": test["covers"],
            "pagerank_score": round(score, 4),
            "top_covered_module": max(covered, key=lambda p: scores.get(p, 0.0), default=None),
        })
    ranked.sort(key=lambda item: item["pagerank_score"], reverse=True)
    top = ranked[0]["path"] if ranked else "none"
    return {
        "repository": analysis["repository"],
        "target": target_path,
        "impact_count": analysis["impact_count"],
        "affected_files": analysis["affected_files"],
        "recommended_tests": analysis["recommended_tests"],
        "change_surface_score": analysis["change_surface_score"],
        "ranked_tests": ranked,
        "algorithm": "algo.pageRank('File', 'IMPORTS')",
        "cypher": {
            "impact_traversal": IMPACT_TRAVERSAL_CYPHER,
            "test_coverage": COVERAGE_CYPHER,
            "pagerank": PAGERANK_CYPHER,
        },
        "timings": {**analysis.get("timings", {}), "pagerank_ms": pagerank_ms},
        "reasoning": (
            f"PageRank over IMPORTS edges scores each module by how much of the repository "
            f"depends on it; tests are ordered by the highest score among the impacted modules "
            f"they cover. Riskiest first: {top}."
        ),
        "tool": "rank_tests_by_pagerank",
        "graph": os.getenv("FALKORDB_GRAPH", "wry_change_impact"),
        "provenance": analysis["provenance"],
    }


mcp = FastMCP("Wry Change Impact Agent", streamable_http_path="/")


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
))
def list_code_targets() -> list[dict]:
    """List modules indexed from the active demo or local Python repository in FalkorDB."""
    return list_targets()


def shortest_dependency_path(from_path: str, to_path: str):
    """Shortest directed dependency chain from one module to another.

    Follows IMPORTS edges (importer -> dependency), so the chain reads as the
    route a change propagates along. Raises ValueError naming the missing
    endpoint when either module is not indexed.
    """
    graph = get_graph()
    _require_module_target(graph, from_path)
    _require_module_target(graph, to_path)
    rows, ms = _timed_ro_query(
        graph, SHORTEST_PATH_CYPHER,
        {"repo": _ACTIVE_REPOSITORY_ID, "from": from_path, "to": to_path},
    )
    if not rows:
        return {
            "from": from_path,
            "to": to_path,
            "connected": False,
            "hops": None,
            "chain": [],
            "reasoning": f"No dependency path from {from_path} to {to_path} within 10 hops in the indexed graph.",
            "tool": "shortest_dependency_path",
            "cypher": {"shortest_path": SHORTEST_PATH_CYPHER},
            "timings": {"shortest_path_ms": ms},
            "provenance": ("Local Python import metadata." if _ACTIVE_DATASET == "local" else "Synthetic dependency edges, seeded idempotently into FalkorDB."),
        }
    chain, hops = rows[0][0], int(rows[0][1])
    return {
        "from": from_path,
        "to": to_path,
        "connected": True,
        "hops": hops,
        "chain": chain,
        "reasoning": (
            f"Shortest dependency path is {hops} hop{'s' if hops != 1 else ''}: "
            f"{' -> '.join(chain)}. A change in {to_path} propagates to {from_path} along this route."
        ),
        "tool": "shortest_dependency_path",
        "cypher": {"shortest_path": SHORTEST_PATH_CYPHER},
        "timings": {"shortest_path_ms": ms},
        "provenance": ("Local Python import metadata." if _ACTIVE_DATASET == "local" else "Synthetic dependency edges, seeded idempotently into FalkorDB."),
    }


def find_bridge_modules(limit: int = 10):
    """Modules with the highest betweenness centrality in the import graph.

    Betweenness scores a module by how many dependency paths pass through it —
    the architectural choke points. Complements PageRank (popularity) with a
    connector-importance signal, via FalkorDB's algo.betweenness.
    """
    graph = get_graph()
    rows, ms = _timed_ro_query(graph, BETWEENNESS_CYPHER, {"repo": _ACTIVE_REPOSITORY_ID})
    bridges = [{"path": row[0], "betweenness_score": round(float(row[1]), 4)} for row in rows[:limit]]
    top = bridges[0]["path"] if bridges else "none"
    return {
        "repository": _ACTIVE_REPOSITORY_NAME,
        "bridges": bridges,
        "algorithm": "algo.betweenness({nodeLabels: ['File'], relationshipTypes: ['IMPORTS']})",
        "cypher": {"betweenness": BETWEENNESS_CYPHER},
        "timings": {"betweenness_ms": ms},
        "reasoning": (
            f"Betweenness over IMPORTS edges scores each module by how many dependency "
            f"paths pass through it. Highest: {top} — a change here, or a break here, "
            f"touches the most routes through the codebase."
        ),
        "tool": "find_bridge_modules",
        "provenance": ("Local Python import metadata." if _ACTIVE_DATASET == "local" else "Synthetic dependency edges, seeded idempotently into FalkorDB."),
    }


def find_impact_communities(target_path: str):
    """Group a change's blast radius into architectural communities.

    Runs FalkorDB's algo.labelPropagation (CDLP) over the import graph, then
    reports which community the changed module belongs to and how many
    distinct communities its transitive impact touches. Complements PageRank
    (which module matters) and betweenness (which module connects) with a
    neighborhood signal: does this change stay local or spill across the
    architecture?
    """
    analysis = trace_change_impact(target_path)
    graph = get_graph()
    rows, community_ms = _timed_ro_query(graph, COMMUNITY_CYPHER, {"repo": _ACTIVE_REPOSITORY_ID})
    community_of: dict[str, int] = {}
    members: dict[int, list[str]] = {}
    for path, community in rows:
        community = int(community)
        community_of[path] = community
        members.setdefault(community, []).append(path)
    impacted = {item["path"] for item in analysis["affected_files"]}
    touched = sorted({community_of[path] for path in impacted if path in community_of})
    communities = [
        {
            "community": cid,
            "size": len(paths),
            "members": sorted(paths),
            "touched": cid in touched,
            "contains_target": target_path in paths,
        }
        for cid, paths in sorted(members.items())
    ]
    target_community = community_of.get(target_path)
    return {
        "repository": analysis["repository"],
        "target": target_path,
        "target_community": target_community,
        "community_count": len(communities),
        "impacted_communities": len(touched),
        "communities": communities,
        "algorithm": "algo.labelPropagation({nodeLabels: ['File'], relationshipTypes: ['IMPORTS']})",
        "cypher": {
            "communities": COMMUNITY_CYPHER,
            "impact_traversal": IMPACT_TRAVERSAL_CYPHER,
        },
        "timings": {**analysis.get("timings", {}), "communities_ms": community_ms},
        "reasoning": (
            f"CDLP community detection over IMPORTS edges groups the repository into "
            f"{len(communities)} architectural communities. {target_path} sits in community "
            f"{target_community}; its change impact touches {len(touched)} of them."
        ),
        "tool": "find_impact_communities",
        "graph": os.getenv("FALKORDB_GRAPH", "wry_change_impact"),
        "provenance": analysis["provenance"],
    }


def record_investigation(target_path: str, tool_name: str, summary: dict):
    """Persist agent memory: what was investigated, when, and the outcome.

    Called by the agent runtime (HTTP layer) after a successful investigation —
    never by the read-only MCP tools themselves, which stay side-effect free.
    """
    graph = get_graph()
    _require_module_target(graph, target_path)
    investigation_id = str(uuid.uuid4())
    graph.query(
        "MATCH (f:File {repository_id:$repo, path:$target}) "
        "CREATE (i:Investigation {id:$id, target:$target, created_at:timestamp(), tool:$tool, "
        "importer_count:$importers, test_count:$tests, top_test:$top_test, summary:$summary}) "
        "MERGE (i)-[:FOR_TARGET]->(f) "
        "RETURN i.id",
        {
            "repo": _ACTIVE_REPOSITORY_ID,
            "id": investigation_id,
            "target": target_path,
            "tool": tool_name,
            "importers": int(summary.get("importer_count", 0)),
            "tests": int(summary.get("test_count", 0)),
            "top_test": summary.get("top_test") or "",
            "summary": summary.get("text", "")[:500],
        },
    )
    return investigation_id


def recall_investigations(target_path: str):
    """Recall the agent's past investigations of a module, newest first.

    This is the read side of the agent's memory: past Investigation nodes are
    stored in FalkorDB itself, so the agent remembers what it learned in
    earlier sessions instead of re-deriving it.
    """
    graph = get_graph()
    _require_module_target(graph, target_path)
    rows, ms = _timed_ro_query(
        graph, RECALL_CYPHER, {"repo": _ACTIVE_REPOSITORY_ID, "target": target_path}
    )
    investigations = [
        {
            "created_at": row[0],
            "tool": row[1],
            "importer_count": row[2],
            "test_count": row[3],
            "top_test": row[4],
            "summary": row[5],
        }
        for row in rows
    ]
    count = len(investigations)
    return {
        "target": target_path,
        "investigation_count": count,
        "investigations": investigations,
        "reasoning": (
            f"The agent remembers {count} past investigation{'s' if count != 1 else ''} "
            f"of {target_path}." if count else
            f"No past investigations of {target_path} — this one will be remembered."
        ),
        "tool": "recall_investigations",
        "cypher": {"recall": RECALL_CYPHER},
        "timings": {"recall_ms": ms},
        "provenance": "Investigation history stored as graph nodes in FalkorDB.",
    }


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
))
def trace_change_impact_tool(target_path: str) -> dict:
    """Trace downstream importers and their covering tests from a changed file using FalkorDB."""
    return trace_change_impact(target_path)


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
))
def rank_tests_pagerank_tool(target_path: str) -> dict:
    """Rank recommended tests by PageRank importance of impacted modules using FalkorDB's algo.pageRank."""
    return rank_tests_by_pagerank(target_path)


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
))
def shortest_dependency_path_tool(from_path: str, to_path: str) -> dict:
    """Find the shortest directed dependency path between two modules using FalkorDB."""
    return shortest_dependency_path(from_path, to_path)


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
))
def find_bridge_modules_tool() -> dict:
    """Find architectural choke points via betweenness centrality using FalkorDB's algo.betweenness."""
    return find_bridge_modules()


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
))
def find_impact_communities_tool(target_path: str) -> dict:
    """Group a change's blast radius into architectural communities via FalkorDB's algo.labelPropagation (CDLP)."""
    return find_impact_communities(target_path)


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
))
def recall_investigations_tool(target_path: str) -> dict:
    """Recall the agent's past investigations of a module from FalkorDB-backed memory, newest first."""
    return recall_investigations(target_path)


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
))
def create_test_plan_tool(target_path: str, rationale: str = "Change impact review",
                          proposal_token: str = "") -> dict:
    """Persist a proposed review and test plan derived from the graph, with tests ordered by PageRank importance; does not edit or execute code.

    Requires a proposal token that the user has approved via POST /api/agent/test-plan; a merely proposed (unapproved) token, or none at all, is refused before any write.
    """
    record = _consume_plan_approval(proposal_token, target_path)
    return create_test_plan(target_path, rationale,
                            approved_tests=record["tests"],
                            test_order=record.get("test_order", "pagerank"))
