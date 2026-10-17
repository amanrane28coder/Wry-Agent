# Wry Change Impact Agent

Wry investigates how a proposed code change propagates through a dependency graph. It uses FalkorDB for graph storage and traversal, exposes graph operations as MCP tools, selects an MCP tool for each request, and can save an explicit proposed test plan back into the graph.

This is a focused prototype for WeMakeDevs Graph Hacks, Track 1: **Agents That Act on Connected Data**. Wry starts with a wholly synthetic Northstar Commerce graph. Optional local Python metadata indexing is disabled by default and may only be enabled for public or otherwise non-restricted data eligible under the hackathon rules. The indexer stores relative paths and import/test relationships, never source contents; Wry does not change source code or execute tests.

## What it demonstrates

- **Multi-hop reasoning:** traverses `IMPORTS` paths up to five hops from a changed module and finds `COVERS` test edges for the resulting change surface.
- **Graph tools through MCP:** provides `list_code_targets`, `trace_change_impact_tool`, `rank_tests_pagerank_tool`, `shortest_dependency_path_tool`, `find_bridge_modules_tool`, `find_impact_communities_tool`, `recall_investigations_tool` (all read-only), and `create_test_plan_tool` (the single write action); the web agent calls these through the MCP client protocol.
- **Honest MCP annotations:** discovery marks investigation tools read-only and the test-plan tool as a non-idempotent write. These are client-facing hints; the application still enforces tool allowlists and authorization itself. Plan writes use a proposal lifecycle: `/api/agent/run` and `/api/agent/investigate` issue a *proposal* token that authorizes nothing; the user's explicit save action (`POST /api/agent/test-plan`) approves it; `create_test_plan_tool` consumes the approved token exactly once (`_consume_plan_approval` in graph_agent.py). A proposal token used directly on the MCP tool - bypassing the REST approval - is refused before any write.
- **Constrained tool choice:** by default a local rule-based router selects between the read-only tools. Optionally enable OpenAI-compatible model tool calling with `LLM_TOOL_SELECTION=true` and `OPENAI_API_KEY`; prompts are sent to that endpoint only in this explicit mode. The model cannot issue Cypher or invoke the write action, and its tool name is validated against an allowlist before MCP execution.
- **Agent loop (Thought -> Action -> Observation):** `POST /api/agent/run` runs a bounded (max 5 steps) loop that chains tools - trace impact, then PageRank ranking, then blast-radius communities (CDLP), then memory recall - narrating each step's real decision rule and summarizing each tool's actual output. The write tool is never executed by the loop; it is proposed with a single-use server approval token and waits for human approval. If a shortest-path request names only one module, the loop asks for the destination (PATH TO) instead of silently substituting impact tracing. The UI renders the loop as an AGENT TRACE timeline plus an approval card.
- **Agent memory in the graph:** every investigation is recorded as an `Investigation` node (`FOR_TARGET` -> the changed file) by the agent runtime - the MCP tools themselves stay read-only. `recall_investigations_tool` reads the history back, so the agent remembers what it learned in earlier sessions instead of re-deriving it.
- **Graph-based action:** only after the user clicks **Save Test Plan** (or approves a proposed plan) does Wry persist a `TestPlan` node with `REVIEW` and `RUN` relationships, tests ordered by PageRank importance.
- **Explainability:** shows the selected tool and why it was selected, the agent trace, dependency paths, test coverage, graph provenance, per-query latency, and the exact action taken.
- **Visible graph proof:** every investigation result includes the exact Cypher queries executed in FalkorDB with per-query timings, shown in the UI under "GRAPH QUERY".
- **Named graph algorithms as agent tools:** `rank_tests_pagerank_tool` runs FalkorDB's `algo.pageRank` over the import graph and orders recommended tests by the PageRank importance of the impacted modules they cover - riskiest first. `find_bridge_modules_tool` runs `algo.betweenness` to surface architectural choke points. `find_impact_communities_tool` runs `algo.labelPropagation` (CDLP) to group the change's blast radius into architectural communities - answering "does this change stay local or spill across the codebase?" `shortest_dependency_path_tool` finds the shortest directed dependency chain between two modules. All are selected via MCP like the other tools.
- **Expected-results evaluation:** `GET /api/agent/evaluate` scores live FalkorDB traversal results against expected importers and tests for five targets (including an unknown-target rejection case), plus a sixth case checking the PageRank test ranking and a seventh checking CDLP community detection, returning per-case diffs and a pass/total summary. Cases are independent read-only queries and run concurrently (ThreadPoolExecutor), with per-case `duration_ms` in the report - about 11s against FalkorDB Cloud instead of ~38s sequential. Expectations live in `EVALUATION_CASES` in `graph_agent.py` and are hard-coded for the Northstar demo graph - so the endpoint refuses with an explanatory SKIPPED reason when a local repository is indexed instead of producing misleading failures. (The CDLP case asserts grouping structure, not exact community ids: label propagation processes nodes in random order, so ids are not stable across runs.) The web UI has a RUN EVALUATION panel that renders the report.
- **Data provenance:** demo and locally indexed files/relationships carry distinct provenance labels.
- **Privacy-gated repository indexing:** off by default. It requires `WRY_ALLOW_REPOSITORY_METADATA_UPLOAD=true`, an explicit UI confirmation that the selected repository is public or otherwise non-restricted, and independent API/indexer checks. Only relative Python paths, labels, and import/test edges are sent; source text stays local. Hidden folders, virtual environments, generated output, symlinks, files over 1 MB, and repositories over 2,000 Python files are skipped. Use the synthetic graph for the hackathon demo unless you have verified the source data is eligible.

## Architecture

`static/` is the single-page interface. `main.py` serves the UI and API, hosts the Streamable HTTP MCP endpoint at `/mcp/`, and dispatches through the MCP tool registry. `graph_agent.py` owns the FalkorDB connection, synthetic graph seeding, the opt-in local Python AST indexer, Cypher queries, and MCP tool definitions.

The graph contains `Repository`, `File`, `TestPlan`, and `Investigation` nodes; `CONTAINS`, `IMPORTS`, `COVERS`, `REVIEW`, `RUN`, and `FOR_TARGET` relationships. File nodes are scoped by repository ID. `IMPORTS` is directed from importer to dependency. Impact paths are displayed from changed target back toward importers.

```
(Repository)-[:CONTAINS]->(File)                 repository owns its files
(File)-[:IMPORTS]->(File)                        importer -> dependency (multi-hop traversal)
(File {kind:'test'})-[:COVERS]->(File)           test -> covered module
(TestPlan)-[:FOR_REPOSITORY]->(Repository)       plan belongs to a repository
(TestPlan)-[:REVIEW]->(File)                     files to review
(TestPlan)-[:RUN]->(File {kind:'test'})          tests to run
(Investigation)-[:FOR_TARGET]->(File)            agent memory: past investigation of a module
```

## Graph queries

The graph is seeded with parameterized `UNWIND` + `MERGE` batches. These read queries are the core of the product; `$target` and `$paths` are bound parameters, never interpolated user text:

```cypher
MATCH p=(consumer:File)-[:IMPORTS*1..5]->(target:File {path:$target})
RETURN consumer.path, length(p), [n IN nodes(p) | n.path]
ORDER BY length(p), consumer.path
```

```cypher
MATCH (test:File {kind:'test'})-[:COVERS]->(covered:File)
WHERE covered.path IN $paths
RETURN test.path, collect(covered.path)
ORDER BY test.path
```

The target criticality is also read from the graph, not a Python-side map:

```cypher
MATCH (f:File {path:$target, kind:'module'})
RETURN f.criticality
```

The named graph algorithm ranks tests by module importance. PageRank scores are
based on incoming `IMPORTS` edges, so a module scores highly when much of the
repository depends on it - a direct blast-radius signal. Scores are computed
over `File`/`IMPORTS` graph-wide and filtered to the active repository:

```cypher
CALL algo.pageRank('File', 'IMPORTS') YIELD node, score
WHERE node.repository_id = $repo
RETURN node.path AS path, score ORDER BY score DESC
```

Each recommended test is ordered by the highest PageRank score among the
impacted modules it covers. The algorithm is exposed as the
`rank_tests_pagerank_tool` MCP tool, selected through the same MCP tool router
as the other tools.

Two more named algorithms serve as agent tools. Betweenness centrality finds
the architectural choke points - modules that lie on the most dependency paths
(a connector-importance signal, complementary to PageRank's popularity signal):

```cypher
CALL algo.betweenness({nodeLabels: ['File'], relationshipTypes: ['IMPORTS']})
YIELD node, score
WHERE node.repository_id = $repo AND node.kind = 'module'
RETURN node.path AS path, score ORDER BY score DESC LIMIT 10
```

Shortest-path answers "why is X affected by the change in Y" with the minimal
directed dependency chain (importer -> dependency):
```cypher
MATCH (a:File {repository_id:$repo, path:$from}), (b:File {repository_id:$repo, path:$to})
WITH a, b, shortestPath((a)-[:IMPORTS*1..10]->(b)) AS p
WHERE p IS NOT NULL
RETURN [n IN nodes(p) | n.path] AS chain, length(p) AS hops
```

Community detection via label propagation (CDLP) answers "does this change stay
local or spill across the architecture?" Densely import-connected modules
converge on a shared community id - the codebase's architectural neighborhoods.
The agent reports which community the changed module sits in and how many
distinct communities its transitive impact touches:

```cypher
CALL algo.labelPropagation({nodeLabels: ['File'], relationshipTypes: ['IMPORTS']})
YIELD node, communityId
WHERE node.repository_id = $repo AND node.kind = 'module'
RETURN node.path AS path, communityId AS community ORDER BY community, path
```

Why these algorithms, in one line each: PageRank ranks tests by the importance
of what they cover (popularity signal); betweenness finds the bridge modules
where a change cascades (connector signal); CDLP groups the blast radius into
architectural neighborhoods (locality signal); shortest-path gives the exact
route a change propagates along. Together they let the agent reason about a
change the way an architect would - what matters, what connects, what clusters,
and what path it takes.
Agent memory is itself graph-native. After each investigation the runtime (not
the read-only MCP tools) records what was learned; recall reads it back:

```cypher
MATCH (f:File {repository_id:$repo, path:$target})
CREATE (i:Investigation {id:$id, target:$target, created_at:timestamp(), tool:$tool,
       importer_count:$importers, test_count:$tests, top_test:$top_test, summary:$summary})
MERGE (i)-[:FOR_TARGET]->(f)
```

```cypher
MATCH (i:Investigation)-[:FOR_TARGET]->(f:File {repository_id:$repo, path:$target})
RETURN i.created_at, i.tool, i.importer_count, i.test_count, i.top_test, i.summary
ORDER BY i.created_at DESC LIMIT 5
```

The explicit user-confirmed write stores the graph-derived proposal and its review/test relationships in one parameterized Cypher operation:

```cypher
MATCH (r:Repository {id:$repo})
CREATE (p:TestPlan {id:$id, target:$target, rationale:$rationale, status:'proposed',
       created_at:timestamp(), provenance:'agent-generated from FalkorDB traversal'})
MERGE (p)-[:FOR_REPOSITORY]->(r)
WITH p UNWIND $review_paths AS path
MATCH (f:File {path:path}) MERGE (p)-[:REVIEW]->(f)
WITH p UNWIND $test_paths AS path
MATCH (t:File {path:path}) MERGE (p)-[:RUN]->(t)
RETURN p.id
```

No graph result means the app fails closed; it does not fabricate impact data.

The repository-surface percentage is `(changed target + unique transitive importers) / indexed modules * 100`, rounded to the nearest whole percent. It is a scope indicator, not a calibrated probability of defects.

## Why the graph is load-bearing (not decoration)

The kill test: disconnect FalkorDB and every capability fails. Tracing, PageRank
ranking, bridge detection, community detection, shortest paths, memory recall,
target listing, evaluation - all are FalkorDB queries; without the graph the
agent cannot answer a single question, let alone act. No result is precomputed,
cached in Python, or derivable from flat storage: multi-hop reachability,
centrality, and community structure are computed inside FalkorDB on every run,
and the exact executed Cypher is shown with each result.

Why not a vector database: embeddings rank by similarity, which answers "what
looks like this change?" Impact analysis needs exact multi-hop reachability
("what transitively imports this module?"), centrality ("which module connects
the most dependency paths?"), and community structure ("does this change stay
local?"). Those are graph computations, not similarity searches - a vector DB
cannot traverse, and traversal is the product.

Code-Graph (FalkorDB's own repo indexer) answers questions *about* code. Wry
*acts* on it: the agent loop traces, ranks, groups, recalls, and then proposes
a concrete test plan that a human approves into the graph. Exploration vs.
action is the difference.

## Limitations

- The demo graph is synthetic (Northstar Commerce): 8 modules, 7 tests,
  hand-built edges. Real repositories are larger and messier; traversal depth
  is capped at 5 hops and shortest-path search at 10.
- Bridge and community detection are structural, not semantic: they see import
  edges, not what the code does. A bridge module is architecturally central,
  not necessarily the riskiest semantic change.
- `IMPORTS` edges are module-to-module only, in both the demo graph and the
  local indexer (test files connect to modules via `COVERS`). Graph algorithms
  over `IMPORTS` - PageRank, betweenness, CDLP - therefore never see test
  nodes, and test files cannot shape module communities or centrality scores.
- CDLP community ids are non-deterministic across runs (random processing
  order); the grouping structure is stable, the numeric labels are not.
- PageRank scores are computed graph-wide over `File`/`IMPORTS` and filtered to
  the active repository; with several indexed repositories the scores are
  relative to the whole graph.
- The agent's tool router is rule-based by default; model-assisted selection is
  opt-in and still constrained to the read-only allowlist - the model never
  writes Cypher.
- Local repository indexing is metadata-only and off by default; it is the
  operator's responsibility to enable it only for public or otherwise
  non-restricted repositories.
- No user authentication: the approval gate enforces the human-in-the-loop
  flow, not remote authorization - anyone who can reach the app can mint a
  proposal token and approve a TestPlan write, or spam Investigation nodes.
  The app is intended for local demo use; do not expose it on a public URL
  without adding authentication and rate limiting.
- The connection indicator reflects FalkorDB reachability at page load; if
  the database disconnects mid-session, the dot can stay green until reload
  (failed requests still show their errors).

## Run locally

Python 3.11 or newer is recommended.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` with the connection details from your FalkorDB Cloud instance. Keep `.env` private; do not put credentials in source control, screenshots, or a public demo. Repository indexing is disabled by default. Only if the selected repository's metadata is public or otherwise non-restricted and eligible under the hackathon rules, set `WRY_REPOSITORY_PATH=/absolute/path/to/repository` and `WRY_ALLOW_REPOSITORY_METADATA_UPLOAD=true`. The UI and server both require explicit authorization; the default synthetic demo works without enabling indexing. Never index a private or restricted repository into the competition graph. Then run:

```sh
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The app shows connection status before enabling graph analysis. The synthetic demo graph is seeded once at app startup (see `seed_demo_graph_at_startup` in `main.py`), which keeps the MCP investigation tools genuinely read-only. If FalkorDB is unreachable at startup, restart the app once the instance is reachable. The MCP endpoint is available at <http://127.0.0.1:8000/mcp/>.

Model-assisted tool selection is optional. It is disabled by default so prompts stay local. If enabled, enter your approved OpenAI-compatible endpoint, model, and key in `.env`; use synthetic demo prompts only. The deterministic local router remains the fallback if the model endpoint is unavailable.

For a local demo instead of Cloud, start FalkorDB on loopback with Docker:

```sh
docker run -d --name wry-falkordb -p 127.0.0.1:6379:6379 falkordb/falkordb:latest
```

In `.env`, set `FALKORDB_HOST=127.0.0.1`, `FALKORDB_PORT=6379`, clear the username/password, set `FALKORDB_SSL=false`, and set `FALKORDB_ALLOW_NO_AUTH=true`. This explicit no-auth mode is intended only for the loopback-bound local container. Restart the web app after changing `.env`.

To use a local unauthenticated FalkorDB instance, omit the password and explicitly set `FALKORDB_ALLOW_NO_AUTH=true`. Do not use that setting with an exposed service.

## Verify

```sh
python -m unittest discover -s tests -v
python -m py_compile main.py graph_agent.py
```

The unit tests use a fake graph and do not require FalkorDB credentials. A live graph smoke test still requires your own instance connection values. Local indexing is tested against temporary fixtures and does not upload the developer's repository during tests.

## AI assistance disclosure

Wry was designed and built by the participant: the original codebase, every feature choice, every design decision, and every verification step. The participant reviewed each AI-produced change line by line - those reviews caught the remaining bugs (approval-token bypass holes, missing PATH TO handling, demo-only evaluation scope, test-file `IMPORTS` edges in both directions) - and ran the test suite and live verifications. Muse (Meta's personal AI agent) assisted with implementation at the participant's request, in two areas:

**1. Independent review (Oct 15, 2026).** Reviewed the participant's codebase against the official hackathon rules - verifying the "4 importers / 5 tests / 62% surface" claims against the fixture data, the new-work rule, and the privacy gate - plus a live review of the running app against the Track 1 "Agents That Act on Connected Data" bar.

**2. Implementation assistance (Oct 15-16, 2026).** The participant chose and specified each improvement; Muse wrote the code: the bounded Thought -> Action -> Observation agent loop (`POST /api/agent/run`); FalkorDB-backed agent memory (`Investigation` nodes); the MCP tool set including the named graph-algorithm tools - PageRank test ranking, betweenness bridge detection, CDLP blast-radius communities, shortest dependency path; the proposal/approval token lifecycle for plan writes (server-enforced, single-use); the expected-results evaluation endpoint and its UI panel; the "Impact Dossier" UI redesign to the participant's direction; the impact-map visualization; and README hardening (kill test, why-not-vector-DB rationale, Limitations). The participant reviewed every change; the final result was live-verified against FalkorDB Cloud (63/63 tests, 7/7 evaluation). Muse also drafted the demo video's narration script; the voiceover is the participant's own recording.

## Submission notes

- Track: Agents That Act on Connected Data.
- FalkorDB is central to analysis and action persistence; MCP exposes the agent's graph tools.
- Demo data are synthetic. Repository metadata indexing is disabled by default and restricted to explicitly authorized public/non-restricted data; source contents are not uploaded.
- Demo video recorded (2:26 walkthrough): multi-hop investigation, tool selection, explainable graph paths, agent memory, and the user-confirmed saved plan, ending on the 7/7 evaluation. Upload it with the submission.
- Disclose AI coding assistance accurately in the submission; review and understand every submitted change.
- Official requirements and the design research log are recorded in [`docs/research.md`](docs/research.md).
- A working local demo still needs a reachable FalkorDB instance configured in `.env`; credentials are intentionally not bundled.
