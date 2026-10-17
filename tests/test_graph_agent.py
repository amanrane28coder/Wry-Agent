import unittest
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import graph_agent


class FakeGraph:
    def __init__(self, path_rows=None, test_rows=None, exists=True, pagerank_rows=None,
                 shortest_rows=None, betweenness_rows=None, recall_rows=None, community_rows=None):
        self.path_rows = path_rows or []
        self.test_rows = test_rows or []
        self.exists = exists
        self.pagerank_rows = pagerank_rows or []
        self.shortest_rows = shortest_rows if shortest_rows is not None else [
            (["src/api/auth_routes.py", "src/core/auth_service.py", "src/core/token_store.py"], 2)]
        self.betweenness_rows = betweenness_rows or []
        self.recall_rows = recall_rows or []
        self.community_rows = community_rows or []
        self.read_calls = 0
        self.writes = []

    def query(self, query, params=None):
        self.writes.append((query, params or {}))
        return None

    def ro_query(self, query, _params=None):
        self.read_calls += 1
        if "algo.pageRank" in query:
            return type("Result", (), {"result_set": self.pagerank_rows})()
        if "algo.betweenness" in query:
            return type("Result", (), {"result_set": self.betweenness_rows})()
        if "algo.labelPropagation" in query:
            return type("Result", (), {"result_set": self.community_rows})()
        if "shortestPath" in query:
            return type("Result", (), {"result_set": self.shortest_rows})()
        if "Investigation" in query:
            return type("Result", (), {"result_set": self.recall_rows})()
        if "IMPORTS*1..5" in query:
            return type("Result", (), {"result_set": self.path_rows})()
        if "COVERS" in query:
            return type("Result", (), {"result_set": self.test_rows})()
        if "count(f)" in query:
            return type("Result", (), {"result_set": [[8]]})()
        if "RETURN f.criticality" in query:
            return type("Result", (), {"result_set": [["medium"]] if self.exists else []})()
        return type("Result", (), {"result_set": [["src/core/token_store.py"]] if self.exists else []})()


class GraphAgentTests(unittest.TestCase):
    def test_impact_trace_returns_reversed_paths_and_coverage(self):
        graph = FakeGraph(
            path_rows=[
                ("src/core/auth_service.py", "critical", 1, ["src/core/auth_service.py", "src/core/token_store.py"]),
                ("src/api/auth_routes.py", "high", 2, ["src/api/auth_routes.py", "src/core/auth_service.py", "src/core/token_store.py"]),
            ],
            test_rows=[("tests/test_auth_service.py", ["src/core/token_store.py"]),
                       ("tests/test_auth_routes.py", ["src/core/auth_service.py"])],
        )
        with patch.object(graph_agent, "get_graph", return_value=graph):
            result = graph_agent.trace_change_impact("src/core/token_store.py")

        self.assertEqual(result["impact_count"], 2)
        self.assertEqual(result["affected_files"][1]["path_nodes"], [
            "src/core/token_store.py", "src/core/auth_service.py"
        ])
        self.assertEqual(result["recommended_tests"][0]["path"], "tests/test_auth_service.py")
        self.assertEqual(result["affected_files"][1]["criticality"], "critical")
        self.assertEqual(result["change_surface_score"], 38)
        self.assertIn("FalkorDB found 2 transitive importers", result["reasoning"])

    def test_demo_graph_load_is_batched_and_idempotent(self):
        graph = FakeGraph()
        graph_agent.seed_demo(graph)
        graph_agent.seed_demo(graph)

        self.assertEqual(len(graph.writes), 10)
        self.assertIn("SET f.repository_id", graph.writes[1][0])
        self.assertTrue(all("MERGE" in query for index, (query, _) in enumerate(graph.writes) if index % 5 != 1))
        self.assertEqual(len(graph.writes[2][1]["files"]), len(graph_agent.FILES))
        self.assertEqual(len(graph.writes[3][1]["edges"]), len(graph_agent.IMPORTS))
        self.assertEqual(len(graph.writes[4][1]["edges"]), len(graph_agent.COVERS))

    def test_user_confirmed_plan_persists_one_graph_action(self):
        graph = FakeGraph(
            path_rows=[("src/core/auth_service.py", "high", 1,
                        ["src/core/auth_service.py", "src/core/token_store.py"])],
            test_rows=[("tests/test_auth_service.py", ["src/core/token_store.py"])],
        )
        with patch.object(graph_agent, "get_graph", return_value=graph):
            result = graph_agent.create_test_plan("src/core/token_store.py", "Review auth path")

        self.assertTrue(result["saved"])
        self.assertEqual(result["tests_to_run"], ["tests/test_auth_service.py"])
        self.assertEqual(result["test_order"], "pagerank")
        self.assertIn("PageRank", result["action"])
        plan_query, plan_params = graph.writes[-1]
        self.assertIn("TestPlan", plan_query)
        self.assertIn("REVIEW", plan_query)
        self.assertIn("RUN", plan_query)
        self.assertEqual(plan_params["rationale"], "Review auth path")

    def test_test_plan_orders_tests_by_pagerank(self):
        graph = FakeGraph(
            path_rows=[
                ("src/core/auth_service.py", "critical", 1,
                 ["src/core/auth_service.py", "src/core/token_store.py"]),
            ],
            test_rows=[("tests/test_token_store.py", ["src/core/token_store.py"]),
                       ("tests/test_auth_service.py", ["src/core/token_store.py", "src/core/auth_service.py"])],
            pagerank_rows=[
                ("src/core/auth_service.py", 0.35),
                ("src/core/token_store.py", 0.20),
            ],
        )
        with patch.object(graph_agent, "get_graph", return_value=graph):
            result = graph_agent.create_test_plan("src/core/token_store.py")

        # recommended_tests arrive token-first, but PageRank puts the
        # auth_service-covering test first in the saved plan.
        self.assertEqual(result["tests_to_run"],
                         ["tests/test_auth_service.py", "tests/test_token_store.py"])

    def test_unknown_target_is_rejected(self):
        graph = FakeGraph(exists=False)
        with patch.object(graph_agent, "get_graph", return_value=graph):
            with self.assertRaisesRegex(ValueError, "not in the indexed repository"):
                graph_agent.trace_change_impact("src/missing.py")

    def test_local_scanner_extracts_only_paths_and_import_edges(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pkg").mkdir()
            (root / "tests").mkdir()
            (root / "pkg" / "core.py").write_text("def token():\n    return 'secret-looking-source'\n")
            (root / "pkg" / "api.py").write_text("from pkg.core import token\n")
            (root / "pkg" / "relative.py").write_text("from .core import token\n")
            (root / "tests" / "test_core.py").write_text("from pkg.core import token\n")
            (root / ".venv").mkdir()
            (root / ".venv" / "ignored.py").write_text("import pkg.core\n")

            files, imports, covers = graph_agent.scan_local_repository(root)

        self.assertEqual({row["path"] for row in files}, {
            "pkg/core.py", "pkg/api.py", "pkg/relative.py", "tests/test_core.py"
        })
        self.assertIn(("pkg/api.py", "pkg/core.py"), imports)
        self.assertIn(("pkg/relative.py", "pkg/core.py"), imports)
        self.assertIn(("tests/test_core.py", "pkg/core.py"), covers)
        self.assertNotIn("secret-looking-source", repr(files))

    def test_local_scanner_excludes_test_files_from_import_edges(self):
        # IMPORTS edges are module-to-module only: test files connect via
        # COVERS, so test nodes never shape IMPORTS-based graph algorithms.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pkg").mkdir()
            (root / "tests").mkdir()
            (root / "pkg" / "core.py").write_text("def token():\n    return 1\n")
            (root / "tests" / "test_core.py").write_text("from pkg.core import token\n")

            _, imports, covers = graph_agent.scan_local_repository(root)

        self.assertNotIn(("tests/test_core.py", "pkg/core.py"), imports)
        self.assertIn(("tests/test_core.py", "pkg/core.py"), covers)
        self.assertTrue(all(not importer.startswith("tests/") for importer, _ in imports))

    def test_local_scanner_excludes_test_helpers_imported_by_modules(self):
        # Reverse direction of the module-to-module invariant: a source
        # module importing a test helper must not emit an IMPORTS edge to
        # the test file, or test nodes would shape IMPORTS-based graph
        # algorithms (PageRank, betweenness, CDLP communities).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pkg").mkdir()
            (root / "tests").mkdir()
            (root / "pkg" / "core.py").write_text(
                "from tests.helpers import make_token\ndef token():\n    return make_token()\n"
            )
            (root / "tests" / "helpers.py").write_text("def make_token():\n    return 1\n")

            _, imports, covers = graph_agent.scan_local_repository(root)

        self.assertNotIn(("pkg/core.py", "tests/helpers.py"), imports)
        self.assertNotIn(("pkg/core.py", "tests/helpers.py"), covers)
        self.assertTrue(all(
            not importer.startswith("tests/") and not dependency.startswith("tests/")
            for importer, dependency in imports
        ))

    def test_local_index_writes_repository_metadata_and_activates_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("SENSITIVE_SOURCE_MARKER = 'local-only'\ndef run(): pass\n")
            (root / "worker.py").write_text("from app import run\n")
            (root / "test_app.py").write_text("from app import run\n")
            original = graph_agent.active_repository_info()
            graph = FakeGraph()
            try:
                with patch.object(graph_agent, "_local_repository_root", return_value=root), \
                        patch.object(graph_agent, "get_graph", return_value=graph), \
                        patch.dict("os.environ", {"WRY_ALLOW_REPOSITORY_METADATA_UPLOAD": "true"}):
                    result = graph_agent.index_local_repository()
                    active = graph_agent.active_repository_info()
            finally:
                graph_agent._ACTIVE_REPOSITORY_ID = original["id"]
                graph_agent._ACTIVE_REPOSITORY_NAME = original["name"]
                graph_agent._ACTIVE_DATASET = original["dataset"]

        self.assertEqual(result["source_files"], 2)
        self.assertEqual(result["test_files"], 1)
        # IMPORTS edges are module-to-module only; the test file connects via COVERS.
        self.assertEqual(result["import_edges"], 1)
        self.assertEqual(result["coverage_edges"], 1)
        self.assertTrue(result["metadata_only"])
        self.assertEqual(active["dataset"], "local")
        self.assertNotIn("SENSITIVE_SOURCE_MARKER", repr(graph.writes))
        self.assertTrue(all("secret" not in repr(params) for _, params in graph.writes))

    def test_local_index_is_disabled_without_eligibility_opt_in(self):
        with patch.dict("os.environ", {"WRY_ALLOW_REPOSITORY_METADATA_UPLOAD": "false"}):
            with self.assertRaisesRegex(ValueError, "public or otherwise non-restricted"):
                graph_agent.index_local_repository()

    def test_request_router_selects_list_or_impact_tool(self):
        self.assertEqual(graph_agent.mcp.name, "Wry Change Impact Agent")
        from main import choose_analysis_tool, select_analysis_tool

        self.assertEqual(select_analysis_tool("list available modules")[0], "list_code_targets")
        self.assertEqual(select_analysis_tool("what depends on this change?")[0], "trace_change_impact_tool")
        with patch("main._model_selection_enabled", return_value=False):
            selected = asyncio.run(choose_analysis_tool("list available modules"))
        self.assertEqual(selected[0], "list_code_targets")
        self.assertEqual(selected[3], "rules")

    def test_mcp_server_publishes_only_expected_graph_tools(self):
        async def read_tools():
            return await graph_agent.mcp.list_tools()

        tools = asyncio.run(read_tools())
        self.assertEqual({tool.name for tool in tools}, {
            "list_code_targets", "trace_change_impact_tool", "rank_tests_pagerank_tool",
            "shortest_dependency_path_tool", "find_bridge_modules_tool",
            "find_impact_communities_tool",
            "recall_investigations_tool", "create_test_plan_tool"
        })
        by_name = {tool.name: tool for tool in tools}
        self.assertTrue(by_name["list_code_targets"].annotations.readOnlyHint)
        self.assertTrue(by_name["trace_change_impact_tool"].annotations.readOnlyHint)
        self.assertTrue(by_name["rank_tests_pagerank_tool"].annotations.readOnlyHint)
        self.assertTrue(by_name["rank_tests_pagerank_tool"].annotations.idempotentHint)
        self.assertTrue(by_name["shortest_dependency_path_tool"].annotations.readOnlyHint)
        self.assertTrue(by_name["find_bridge_modules_tool"].annotations.readOnlyHint)
        self.assertTrue(by_name["find_impact_communities_tool"].annotations.readOnlyHint)
        self.assertTrue(by_name["find_impact_communities_tool"].annotations.idempotentHint)
        self.assertTrue(by_name["recall_investigations_tool"].annotations.readOnlyHint)
        self.assertFalse(by_name["create_test_plan_tool"].annotations.readOnlyHint)
        self.assertFalse(by_name["create_test_plan_tool"].annotations.idempotentHint)

    def test_model_choice_is_allowlisted_and_reasoned(self):
        from main import choose_analysis_tool

        response = {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "trace_change_impact_tool",
            "arguments": '{"reason":"The request asks which modules depend on this change."}',
        }}]}}]}
        with patch("main._model_selection_enabled", return_value=True), \
                patch("main._request_tool_selection", return_value=response):
            selected = asyncio.run(choose_analysis_tool("What breaks if this changes?"))
        self.assertEqual(selected[0], "trace_change_impact_tool")
        self.assertEqual(selected[2], "The request asks which modules depend on this change.")
        self.assertEqual(selected[3], "model")

    def test_unknown_model_tool_falls_back_to_allowlisted_local_router(self):
        from main import choose_analysis_tool

        response = {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "execute_arbitrary_cypher", "arguments": '{"reason":"bad"}'
        }}]}}]}
        with patch("main._model_selection_enabled", return_value=True), \
                patch("main._request_tool_selection", return_value=response):
            selected = asyncio.run(choose_analysis_tool("What breaks if this changes?"))
        self.assertEqual(selected[0], "trace_change_impact_tool")
        self.assertEqual(selected[3], "rules_fallback")


    def test_run_evaluation_passes_when_graph_matches_expectations(self):
        def fake_trace(target):
            case = next(c for c in graph_agent.EVALUATION_CASES if c["target"] == target)
            if case.get("expect_error"):
                raise ValueError(f"'{target}' is not in the indexed repository.")
            affected = [{"path": target, "distance": 0}]
            affected += [{"path": path, "distance": 1} for path in case["expected_importers"]]
            return {
                "affected_files": affected,
                "recommended_tests": [{"path": path} for path in case["expected_tests"]],
                "change_surface_score": 50,
            }

        fake_ranking = {"ranked_tests": [
            {"path": "tests/test_auth_service.py", "pagerank_score": 0.35,
             "top_covered_module": "src/core/auth_service.py"},
            {"path": "tests/test_token_store.py", "pagerank_score": 0.20,
             "top_covered_module": "src/core/token_store.py"},
        ]}
        fake_communities = {
            "target": "src/core/token_store.py",
            "target_community": 2,
            "community_count": 3,
            "impacted_communities": 2,
            "communities": [
                {"community": 0, "size": 2, "members": ["src/checkout/checkout.py"],
                 "touched": True, "contains_target": False},
                {"community": 2, "size": 3,
                 "members": ["src/core/auth_service.py", "src/core/token_store.py"],
                 "touched": True, "contains_target": True},
                {"community": 5, "size": 1, "members": ["src/notifications/email.py"],
                 "touched": False, "contains_target": False},
            ],
        }
        with patch.object(graph_agent, "trace_change_impact", side_effect=fake_trace), \
                patch.object(graph_agent, "rank_tests_by_pagerank", return_value=fake_ranking), \
                patch.object(graph_agent, "find_impact_communities", return_value=fake_communities):
            report = graph_agent.run_evaluation()

        self.assertEqual(report["total"], len(graph_agent.EVALUATION_CASES) + 2)
        self.assertEqual(report["passed"], report["total"])
        self.assertTrue(all(case["passed"] for case in report["cases"]))
        self.assertIn("duration_ms", report)
        self.assertTrue(all("duration_ms" in case for case in report["cases"]))
        pagerank_case = next(case for case in report["cases"] if case.get("kind") == "pagerank")
        self.assertEqual(pagerank_case["actual_top_module"], "src/core/auth_service.py")
        communities_case = next(case for case in report["cases"] if case.get("kind") == "communities")
        self.assertEqual(communities_case["target_community"], 2)
        self.assertEqual(communities_case["impacted_communities"], 2)

    def test_run_evaluation_reports_importer_and_test_mismatches(self):
        def fake_trace(target):
            if target == "src/missing.py":
                raise ValueError("'src/missing.py' is not in the indexed repository.")
            return {
                "affected_files": [
                    {"path": "src/core/token_store.py", "distance": 0},
                    {"path": "src/core/auth_service.py", "distance": 1},
                ],
                "recommended_tests": [
                    {"path": "tests/test_auth_service.py"},
                    {"path": "tests/test_bogus.py"},
                ],
                "change_surface_score": 25,
            }

        fake_ranking = {"ranked_tests": [
            {"path": "tests/test_auth_service.py", "pagerank_score": 0.35,
             "top_covered_module": "src/core/auth_service.py"},
        ]}
        fake_communities = {
            "target": "src/core/token_store.py", "target_community": 1,
            "community_count": 2, "impacted_communities": 1,
            "communities": [
                {"community": 1, "size": 2,
                 "members": ["src/core/auth_service.py", "src/core/token_store.py"],
                 "touched": True, "contains_target": True},
            ],
        }
        with patch.object(graph_agent, "trace_change_impact", side_effect=fake_trace), \
                patch.object(graph_agent, "rank_tests_by_pagerank", return_value=fake_ranking), \
                patch.object(graph_agent, "find_impact_communities", return_value=fake_communities):
            report = graph_agent.run_evaluation()

        by_target = {case["target"]: case for case in report["cases"] if not case.get("kind")}
        token_case = by_target["src/core/token_store.py"]
        self.assertFalse(token_case["passed"])
        self.assertIn("src/api/auth_routes.py", token_case["missing_importers"])
        self.assertIn("tests/test_bogus.py", token_case["unexpected_tests"])
        self.assertIn("tests/test_token_store.py", token_case["missing_tests"])
        self.assertTrue(by_target["src/missing.py"]["passed"])

    def test_run_evaluation_pagerank_case_fails_cleanly_on_graph_error(self):
        with patch.object(graph_agent, "trace_change_impact",
                          side_effect=ValueError("boom")), \
                patch.object(graph_agent, "rank_tests_by_pagerank",
                             side_effect=RuntimeError("procedure unavailable")):
            report = graph_agent.run_evaluation()

        pagerank_case = next(case for case in report["cases"] if case.get("kind") == "pagerank")
        self.assertFalse(pagerank_case["passed"])
        self.assertIn("procedure unavailable", pagerank_case["error"])
        self.assertEqual(report["total"], len(graph_agent.EVALUATION_CASES) + 2)

    def test_shortest_dependency_path_returns_chain_and_hops(self):
        graph = FakeGraph()
        with patch.object(graph_agent, "get_graph", return_value=graph):
            result = graph_agent.shortest_dependency_path(
                "src/api/auth_routes.py", "src/core/token_store.py")

        self.assertTrue(result["connected"])
        self.assertEqual(result["hops"], 2)
        self.assertEqual(result["chain"],
                         ["src/api/auth_routes.py", "src/core/auth_service.py", "src/core/token_store.py"])
        self.assertIn("shortestPath", result["cypher"]["shortest_path"])
        self.assertIn("shortest_path_ms", result["timings"])

    def test_shortest_dependency_path_reports_disconnected(self):
        graph = FakeGraph(shortest_rows=[])
        with patch.object(graph_agent, "get_graph", return_value=graph):
            result = graph_agent.shortest_dependency_path(
                "src/api/auth_routes.py", "src/users/profile.py")

        self.assertFalse(result["connected"])
        self.assertIsNone(result["hops"])
        self.assertEqual(result["chain"], [])

    def test_shortest_dependency_path_rejects_unknown_endpoint(self):
        graph = FakeGraph(exists=False)
        with patch.object(graph_agent, "get_graph", return_value=graph):
            with self.assertRaisesRegex(ValueError, "not in the indexed repository"):
                graph_agent.shortest_dependency_path("src/missing.py", "src/core/token_store.py")

    def test_find_bridge_modules_orders_by_betweenness(self):
        graph = FakeGraph(betweenness_rows=[
            ("src/core/auth_service.py", 12.5),
            ("src/core/token_store.py", 4.0),
        ])
        with patch.object(graph_agent, "get_graph", return_value=graph):
            result = graph_agent.find_bridge_modules()

        self.assertEqual([b["path"] for b in result["bridges"]],
                         ["src/core/auth_service.py", "src/core/token_store.py"])
        self.assertEqual(result["bridges"][0]["betweenness_score"], 12.5)
        self.assertIn("algo.betweenness", result["algorithm"])

    def test_find_impact_communities_groups_blast_radius(self):
        graph = FakeGraph(
            path_rows=[
                ("src/core/auth_service.py", "critical", 1,
                 ["src/core/auth_service.py", "src/core/token_store.py"]),
            ],
            test_rows=[],
            community_rows=[
                ("src/core/token_store.py", 2),
                ("src/core/auth_service.py", 2),
                ("src/api/auth_routes.py", 2),
                ("src/notifications/email.py", 7),
            ],
        )
        with patch.object(graph_agent, "get_graph", return_value=graph):
            result = graph_agent.find_impact_communities("src/core/token_store.py")

        self.assertEqual(result["target_community"], 2)
        self.assertEqual(result["community_count"], 2)
        # target + auth_service are impacted and share community 2
        self.assertEqual(result["impacted_communities"], 1)
        target_entry = next(c for c in result["communities"] if c["contains_target"])
        self.assertEqual(target_entry["community"], 2)
        self.assertIn("src/core/auth_service.py", target_entry["members"])
        untouched = next(c for c in result["communities"] if not c["touched"])
        self.assertEqual(untouched["community"], 7)
        self.assertIn("algo.labelPropagation", result["algorithm"])
        self.assertIn("labelPropagation", result["cypher"]["communities"])
        self.assertIn("communities_ms", result["timings"])

    def test_find_impact_communities_rejects_unknown_target(self):
        graph = FakeGraph(exists=False)
        with patch.object(graph_agent, "get_graph", return_value=graph):
            with self.assertRaises(ValueError):
                graph_agent.find_impact_communities("src/missing.py")

    def test_run_evaluation_communities_case_fails_cleanly_on_graph_error(self):
        with patch.object(graph_agent, "find_impact_communities",
                          side_effect=RuntimeError("procedure unavailable")):
            case = graph_agent._evaluate_communities()

        self.assertFalse(case["passed"])
        self.assertEqual(case["kind"], "communities")
        self.assertEqual(case["target"], "src/core/token_store.py")
        self.assertIn("procedure unavailable", case["error"])
        self.assertIn("duration_ms", case)

    def test_run_evaluation_communities_case_checks_grouping_structure(self):
        fake = {
            "target": "src/core/token_store.py", "target_community": 4,
            "community_count": 2, "impacted_communities": 2,
            "communities": [
                {"community": 4, "size": 5, "members": ["src/core/token_store.py"],
                 "touched": True, "contains_target": True},
                {"community": 9, "size": 3, "members": ["src/notifications/email.py"],
                 "touched": True, "contains_target": False},
            ],
        }
        with patch.object(graph_agent, "find_impact_communities", return_value=fake):
            case = graph_agent._evaluate_communities()

        self.assertTrue(case["passed"])
        self.assertEqual(case["target_community"], 4)
        # community ids are not asserted for exact values beyond placement:
        # CDLP ids are non-deterministic across runs
        self.assertEqual(case["community_count"], 2)

    def test_request_router_selects_communities_tool(self):
        from main import select_analysis_tool

        self.assertEqual(select_analysis_tool("which blast radius communities does this touch?")[0],
                         "find_impact_communities_tool")
        self.assertEqual(select_analysis_tool("show me the module clusters")[0],
                         "find_impact_communities_tool")
        # existing behavior unchanged
        self.assertEqual(select_analysis_tool("rank the riskiest tests first")[0],
                         "rank_tests_pagerank_tool")

    def test_model_choice_allows_communities_tool(self):
        from main import choose_analysis_tool

        response = {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "find_impact_communities_tool",
            "arguments": '{"reason":"The request asks about blast-radius communities."}',
        }}]}}]}
        with patch("main._model_selection_enabled", return_value=True), \
                patch("main._request_tool_selection", return_value=response):
            selected = asyncio.run(choose_analysis_tool("Which communities does this change touch?"))
        self.assertEqual(selected[0], "find_impact_communities_tool")
        self.assertEqual(selected[3], "model")

    def test_investigate_remembers_communities_tool(self):
        from main import _summarize_for_memory

        summary = _summarize_for_memory("find_impact_communities_tool", {
            "impacted_communities": 2, "community_count": 5, "target_community": 3})
        self.assertIn("2 of 5", summary["text"])
        self.assertIn("community 3", summary["text"])

    def test_agent_loop_communities_first_tool(self):
        from main import run_agent_loop

        async def fake_call_tool(name, arguments):
            if name == "find_impact_communities_tool":
                return {"target": "src/core/token_store.py", "target_community": 2,
                        "community_count": 3, "impacted_communities": 2,
                        "communities": [{"community": 2, "size": 2, "members": [],
                                         "touched": True, "contains_target": True}],
                        "tool": "find_impact_communities"}
            if name == "recall_investigations_tool":
                return {"investigation_count": 0, "investigations": []}
            raise AssertionError(f"unexpected tool call: {name}")

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1"):
            result = asyncio.run(run_agent_loop(
                "which communities does this change touch?", "src/core/token_store.py"))

        actions = [step["action"] for step in result["steps"]]
        self.assertEqual(actions, ["find_impact_communities_tool", "recall_investigations_tool"])
        self.assertEqual(result["result"]["tool"], "find_impact_communities")
        self.assertIn("2 of them", result["final_answer"])

    def test_record_and_recall_investigations(self):
        graph = FakeGraph(recall_rows=[
            (1700000000000, "trace_change_impact_tool", 4, 5,
             "tests/test_auth_service.py", "4 importers, 5 tests"),
        ])
        with patch.object(graph_agent, "get_graph", return_value=graph):
            investigation_id = graph_agent.record_investigation(
                "src/core/token_store.py", "trace_change_impact_tool",
                {"importer_count": 4, "test_count": 5,
                 "top_test": "tests/test_auth_service.py", "text": "4 importers, 5 tests"})
            recalled = graph_agent.recall_investigations("src/core/token_store.py")

        write_query, write_params = graph.writes[-1]
        self.assertIn("Investigation", write_query)
        self.assertIn("FOR_TARGET", write_query)
        self.assertEqual(write_params["id"], investigation_id)
        self.assertEqual(write_params["importers"], 4)
        self.assertEqual(recalled["investigation_count"], 1)
        self.assertEqual(recalled["investigations"][0]["top_test"], "tests/test_auth_service.py")
        self.assertIn("remembers 1 past investigation", recalled["reasoning"])

    def test_recall_investigations_empty_memory_message(self):
        graph = FakeGraph()
        with patch.object(graph_agent, "get_graph", return_value=graph):
            recalled = graph_agent.recall_investigations("src/core/token_store.py")

        self.assertEqual(recalled["investigation_count"], 0)
        self.assertIn("No past investigations", recalled["reasoning"])

    def test_recall_investigations_rejects_unknown_target(self):
        graph = FakeGraph(exists=False)
        with patch.object(graph_agent, "get_graph", return_value=graph):
            with self.assertRaisesRegex(ValueError, "not in the indexed repository"):
                graph_agent.recall_investigations("src/missing.py")

    def test_trace_result_includes_query_timings(self):
        graph = FakeGraph(
            path_rows=[("src/core/auth_service.py", "critical", 1,
                        ["src/core/auth_service.py", "src/core/token_store.py"])],
            test_rows=[("tests/test_auth_service.py", ["src/core/token_store.py"])],
        )
        with patch.object(graph_agent, "get_graph", return_value=graph):
            result = graph_agent.trace_change_impact("src/core/token_store.py")

        self.assertIn("impact_traversal_ms", result["timings"])
        self.assertIn("test_coverage_ms", result["timings"])
        self.assertGreaterEqual(result["timings"]["impact_traversal_ms"], 0)

    def test_trace_result_includes_executed_cypher(self):
        graph = FakeGraph(
            path_rows=[("src/core/auth_service.py", "critical", 1,
                        ["src/core/auth_service.py", "src/core/token_store.py"])],
            test_rows=[("tests/test_auth_service.py", ["src/core/token_store.py"])],
        )
        with patch.object(graph_agent, "get_graph", return_value=graph):
            result = graph_agent.trace_change_impact("src/core/token_store.py")

        self.assertIn("IMPORTS*1..5", result["cypher"]["impact_traversal"])
        self.assertIn("COVERS", result["cypher"]["test_coverage"])
        self.assertEqual(result["cypher"]["impact_traversal"], graph_agent.IMPACT_TRAVERSAL_CYPHER)

    def test_evaluate_endpoint_delegates_to_run_evaluation(self):
        from main import evaluate

        report = {"passed": 5, "total": 5, "cases": []}
        with patch("main.graph_status", return_value={"configured": True}), \
                patch("main.run_evaluation", return_value=report) as mock_evaluate:
            result = asyncio.run(evaluate())

        mock_evaluate.assert_called_once_with()
        self.assertEqual(result, report)

    def test_pagerank_ranking_orders_tests_by_module_importance(self):
        graph = FakeGraph(
            path_rows=[
                ("src/core/auth_service.py", "critical", 1,
                 ["src/core/auth_service.py", "src/core/token_store.py"]),
                ("src/api/auth_routes.py", "high", 2,
                 ["src/api/auth_routes.py", "src/core/auth_service.py", "src/core/token_store.py"]),
            ],
            test_rows=[("tests/test_auth_service.py", ["src/core/token_store.py", "src/core/auth_service.py"]),
                       ("tests/test_token_store.py", ["src/core/token_store.py"])],
            pagerank_rows=[
                ("src/core/auth_service.py", 0.35),
                ("src/core/token_store.py", 0.20),
                ("src/api/auth_routes.py", 0.05),
            ],
        )
        with patch.object(graph_agent, "get_graph", return_value=graph):
            result = graph_agent.rank_tests_by_pagerank("src/core/token_store.py")

        ranked = result["ranked_tests"]
        self.assertEqual([item["path"] for item in ranked],
                         ["tests/test_auth_service.py", "tests/test_token_store.py"])
        self.assertEqual(ranked[0]["pagerank_score"], 0.35)
        self.assertEqual(ranked[0]["top_covered_module"], "src/core/auth_service.py")
        self.assertEqual(ranked[1]["pagerank_score"], 0.20)
        self.assertIn("algo.pageRank", result["algorithm"])
        self.assertIn("algo.pageRank", result["cypher"]["pagerank"])
        self.assertIn("Riskiest first", result["reasoning"])
        # The agent loop displays the rank result; it must carry the surface
        # score through or the UI renders "—" for repository surface.
        self.assertEqual(result["change_surface_score"], 38)

    def test_pagerank_tool_rejects_unknown_target(self):
        graph = FakeGraph(exists=False)
        with patch.object(graph_agent, "get_graph", return_value=graph):
            with self.assertRaisesRegex(ValueError, "not in the indexed repository"):
                graph_agent.rank_tests_by_pagerank("src/missing.py")

    def test_request_router_selects_pagerank_tool_for_ranking(self):
        from main import select_analysis_tool

        self.assertEqual(select_analysis_tool("rank the riskiest tests first")[0],
                         "rank_tests_pagerank_tool")
        self.assertEqual(select_analysis_tool("which tests should I prioritize?")[0],
                         "rank_tests_pagerank_tool")
        # existing behavior unchanged
        self.assertEqual(select_analysis_tool("list available modules")[0], "list_code_targets")
        self.assertEqual(select_analysis_tool("what depends on this change?")[0], "trace_change_impact_tool")

    def test_request_router_selects_new_tools(self):
        from main import select_analysis_tool

        self.assertEqual(select_analysis_tool("shortest path between auth and token store")[0],
                         "shortest_dependency_path_tool")
        self.assertEqual(select_analysis_tool("which modules are the biggest bottlenecks?")[0],
                         "find_bridge_modules_tool")
        self.assertEqual(select_analysis_tool("have we investigated this module before?")[0],
                         "recall_investigations_tool")

    def test_request_router_routes_impact_question_to_trace_not_list(self):
        from main import select_analysis_tool

        # Regression: "show"/"files" must not hijack an impact question into a
        # module listing -- impact/change words beat the list rule.
        tool, _, _ = select_analysis_tool(
            "Show me which files are affected by changing auth_service")
        self.assertEqual(tool, "trace_change_impact_tool")
        # ... while plain listing prompts still list.
        self.assertEqual(select_analysis_tool("show me the available modules")[0],
                         "list_code_targets")

    def test_request_tool_selection_builds_real_urllib_request(self):
        # Regression: urllib's Request was shadowed by fastapi.Request, so the
        # model call TypeError'd and silently fell back to rules. This test
        # exercises the real request construction against a fake urlopen.
        from main import _request_tool_selection

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"choices": []}'

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["data"] = request.data
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            return FakeResponse()

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), \
                patch("main.urlopen", side_effect=fake_urlopen):
            result = _request_tool_selection("https://api.openai.com/v1", {"model": "gpt-x"})

        self.assertEqual(captured["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(json.loads(captured["data"]), {"model": "gpt-x"})
        self.assertEqual(captured["headers"]["authorization"], "Bearer test-key")
        self.assertEqual(captured["headers"]["content-type"], "application/json")
        self.assertEqual(result, {"choices": []})

    def test_model_choice_allows_pagerank_tool(self):
        from main import choose_analysis_tool

        response = {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "rank_tests_pagerank_tool",
            "arguments": '{"reason":"The request asks to rank tests by importance."}',
        }}]}}]}
        with patch("main._model_selection_enabled", return_value=True), \
                patch("main._request_tool_selection", return_value=response):
            selected = asyncio.run(choose_analysis_tool("Rank the tests by risk"))
        self.assertEqual(selected[0], "rank_tests_pagerank_tool")
        self.assertEqual(selected[3], "model")

    def test_agent_loop_chains_trace_rank_recall_and_proposes_plan(self):
        from main import run_agent_loop

        async def fake_call_tool(name, arguments):
            if name == "trace_change_impact_tool":
                return {"impact_count": 4,
                        "recommended_tests": [{"path": "tests/test_a.py"}, {"path": "tests/test_b.py"}],
                        "change_surface_score": 62}
            if name == "rank_tests_pagerank_tool":
                return {"ranked_tests": [
                    {"path": "tests/test_a.py", "pagerank_score": 0.35},
                    {"path": "tests/test_b.py", "pagerank_score": 0.20}]}
            if name == "find_impact_communities_tool":
                return {"target_community": 2, "community_count": 3, "impacted_communities": 2,
                        "communities": []}
            if name == "recall_investigations_tool":
                return {"investigation_count": 0, "investigations": []}
            raise AssertionError(f"unexpected tool call: {name}")

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1"):
            result = asyncio.run(run_agent_loop(
                "Investigate the change, rank the riskiest tests, and save a plan",
                "src/core/token_store.py"))

        actions = [step["action"] for step in result["steps"]]
        self.assertEqual(actions, ["trace_change_impact_tool", "rank_tests_pagerank_tool",
                                  "find_impact_communities_tool", "recall_investigations_tool",
                                  "proposed:create_test_plan_tool"])
        # the write tool is proposed, never executed by the loop
        self.assertIsNotNone(result["proposed_action"])
        self.assertEqual(result["proposed_action"]["tool"], "create_test_plan_tool")
        self.assertEqual(result["proposed_action"]["tests"], ["tests/test_a.py", "tests/test_b.py"])
        self.assertTrue(all("thought" in step and "observation" in step for step in result["steps"]))
        self.assertIn("4 transitive importers", result["final_answer"])
        self.assertIn("tests/test_a.py", result["final_answer"])
        self.assertIn("2 of them", result["final_answer"])
        # the blast-radius analysis is merged into the display result for the UI
        self.assertEqual(result["result"]["communities"]["impacted_communities"], 2)

    def test_agent_loop_returns_recall_result_for_memory_panel(self):
        from main import run_agent_loop

        async def fake_call_tool(name, arguments):
            if name == "trace_change_impact_tool":
                return {"target": "src/core/token_store.py", "impact_count": 4,
                        "affected_files": [], "recommended_tests": [{"path": "tests/test_a.py"}],
                        "change_surface_score": 38, "tool": "trace_change_impact",
                        "cypher": {"trace": "MATCH (t:File)<-[:IMPORTS*1..5]-(d:File)"},
                        "timings": {"trace_ms": 3}}
            if name == "rank_tests_pagerank_tool":
                return {"target": "src/core/token_store.py",
                        "ranked_tests": [{"path": "tests/test_a.py", "pagerank_score": 0.35}],
                        "change_surface_score": 38, "tool": "rank_tests_pagerank",
                        "cypher": {"pagerank": "CALL algo.pageRank"},
                        "timings": {"pagerank_ms": 4}}
            if name == "find_impact_communities_tool":
                return {"target": "src/core/token_store.py", "impacted_communities": 2,
                        "tool": "find_impact_communities",
                        "cypher": {"communities": "CALL algo.labelPropagation"},
                        "timings": {"communities_ms": 5}}
            if name == "recall_investigations_tool":
                return {"target": "src/core/token_store.py", "investigation_count": 1,
                        "investigations": [{"created_at": 1, "tool": "trace_change_impact_tool",
                                            "importer_count": 4, "test_count": 1,
                                            "top_test": "tests/test_a.py", "summary": "earlier look"}],
                        "tool": "recall_investigations",
                        "cypher": {"recall": "MATCH (i:Investigation)"},
                        "timings": {"recall_ms": 2}}
            raise AssertionError(f"unexpected tool call: {name}")

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1"):
            result = asyncio.run(run_agent_loop(
                "Investigate the change and rank the riskiest tests",
                "src/core/token_store.py"))

        actions = [step["action"] for step in result["steps"]]
        self.assertEqual(actions, ["trace_change_impact_tool", "rank_tests_pagerank_tool",
                                  "find_impact_communities_tool", "recall_investigations_tool"])
        # the recall payload is returned separately so the loop response can
        # drive the AGENT MEMORY panel -- not just the trace observation
        self.assertEqual(result["past_investigations"]["tool"], "recall_investigations")
        self.assertEqual(result["past_investigations"]["investigation_count"], 1)
        # and its Cypher/timings merge into the display result for GRAPH QUERY,
        # while the primary investigation payload stays the display result
        self.assertEqual(result["result"]["tool"], "rank_tests_pagerank")
        self.assertIn("recall", result["result"]["cypher"])
        self.assertIn("recall_ms", result["result"]["timings"])

    def test_agent_loop_records_communities_first_investigation(self):
        # A communities-first run recalls history but has no trace_result;
        # it must still leave its own finding in memory.
        from main import run_agent_loop

        async def fake_call_tool(name, arguments):
            if name == "find_impact_communities_tool":
                return {"target": "src/core/token_store.py", "target_community": 2,
                        "community_count": 3, "impacted_communities": 2,
                        "communities": [], "tool": "find_impact_communities"}
            if name == "recall_investigations_tool":
                return {"investigation_count": 0, "investigations": []}
            raise AssertionError(f"unexpected tool call: {name}")

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1") as mock_record:
            result = asyncio.run(run_agent_loop(
                "which communities does this change touch?", "src/core/token_store.py"))

        actions = [step["action"] for step in result["steps"]]
        self.assertEqual(actions, ["find_impact_communities_tool", "recall_investigations_tool"])
        mock_record.assert_called_once()
        summary = mock_record.call_args[0][2]
        self.assertIn("2 of 3", summary["text"])

    def test_agent_loop_records_shortest_path_investigation(self):
        from main import run_agent_loop

        async def fake_call_tool(name, arguments):
            if name == "shortest_dependency_path_tool":
                return {"connected": True, "hops": 2, "from": "a.py", "to": "b.py",
                        "chain": ["a.py", "m.py", "b.py"], "tool": "shortest_dependency_path"}
            if name == "recall_investigations_tool":
                return {"investigation_count": 0, "investigations": []}
            raise AssertionError(f"unexpected tool call: {name}")

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1") as mock_record:
            result = asyncio.run(run_agent_loop("shortest path", "a.py", other_path="b.py"))

        mock_record.assert_called_once()
        summary = mock_record.call_args[0][2]
        self.assertIn("2 hops", summary["text"])

    def test_bridge_and_shortest_path_provenance_follows_dataset(self):
        graph = FakeGraph(betweenness_rows=[("src/core/auth_service.py", 12.5)])
        with patch.object(graph_agent, "get_graph", return_value=graph), \
                patch.object(graph_agent, "_ACTIVE_DATASET", "local"):
            bridge = graph_agent.find_bridge_modules()
            path = graph_agent.shortest_dependency_path(
                "src/api/auth_routes.py", "src/core/token_store.py")
        self.assertIn("Local", bridge["provenance"])
        self.assertNotIn("Synthetic", bridge["provenance"])
        self.assertIn("Local", path["provenance"])
        self.assertNotIn("Synthetic", path["provenance"])
        with patch.object(graph_agent, "get_graph", return_value=graph):
            bridge_demo = graph_agent.find_bridge_modules()
        self.assertIn("Synthetic", bridge_demo["provenance"])

    def test_agent_loop_records_rank_summary_when_ranked(self):
        # Memory must match the displayed result: a trace+rank run shows the
        # PageRank ranking, so the stored summary is the rank one, not trace.
        from main import run_agent_loop

        async def fake_call_tool(name, arguments):
            if name == "trace_change_impact_tool":
                return {"target": "src/core/token_store.py", "impact_count": 4,
                        "recommended_tests": [{"path": "tests/test_b.py"},
                                              {"path": "tests/test_a.py"}],
                        "tool": "trace_change_impact"}
            if name == "rank_tests_pagerank_tool":
                return {"target": "src/core/token_store.py",
                        "ranked_tests": [{"path": "tests/test_a.py", "pagerank_score": 0.35},
                                         {"path": "tests/test_b.py", "pagerank_score": 0.20}],
                        "tool": "rank_tests_pagerank"}
            if name == "find_impact_communities_tool":
                return {"target": "src/core/token_store.py", "impacted_communities": 1,
                        "tool": "find_impact_communities"}
            if name == "recall_investigations_tool":
                return {"investigation_count": 0, "investigations": []}
            raise AssertionError(f"unexpected tool call: {name}")

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1") as mock_record:
            result = asyncio.run(run_agent_loop(
                "Investigate the change and rank the riskiest tests",
                "src/core/token_store.py"))

        # the displayed result is the PageRank ranking ...
        self.assertEqual(result["result"]["tool"], "rank_tests_pagerank")
        # ... and the stored summary is the rank one (top_test_a), not trace's
        mock_record.assert_called_once()
        summary = mock_record.call_args[0][2]
        self.assertEqual(summary["top_test"], "tests/test_a.py")

    def test_agent_loop_listing_is_single_step(self):
        from main import run_agent_loop

        async def fake_call_tool(name, arguments):
            self.assertEqual(name, "list_code_targets")
            return [{"path": "a.py"}]

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.choose_analysis_tool",
                      return_value=("list_code_targets", {}, "listing", "rules")), \
                patch("main.record_investigation") as mock_record:
            result = asyncio.run(run_agent_loop("list available modules", "src/core/token_store.py"))

        self.assertEqual(len(result["steps"]), 1)
        self.assertIsNone(result["proposed_action"])
        mock_record.assert_not_called()

    def test_agent_loop_shortest_path_when_other_path_given(self):
        from main import run_agent_loop

        seen = []

        async def fake_call_tool(name, arguments):
            seen.append((name, arguments))
            if name == "shortest_dependency_path_tool":
                return {"connected": True, "hops": 2, "from": arguments["from_path"],
                        "to": arguments["to_path"],
                        "chain": [arguments["from_path"], "src/core/auth_service.py", arguments["to_path"]]}
            return {"investigation_count": 0, "investigations": []}

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1"):
            result = asyncio.run(run_agent_loop(
                "show the path", "src/api/auth_routes.py", other_path="src/core/token_store.py"))

        self.assertEqual(result["steps"][0]["action"], "shortest_dependency_path_tool")
        self.assertEqual(seen[0][1]["to_path"], "src/core/token_store.py")
        self.assertIn("2 hops", result["steps"][0]["observation"])

    def test_agent_loop_preserves_shortest_path_display_result(self):
        # The trailing memory-recall step must not overwrite the shortest-path
        # payload: the UI renders the display result from its tool marker.
        from main import run_agent_loop

        async def fake_call_tool(name, arguments):
            if name == "shortest_dependency_path_tool":
                return {"connected": True, "hops": 2, "from": arguments["from_path"],
                        "to": arguments["to_path"],
                        "chain": [arguments["from_path"], "src/core/auth_service.py",
                                  arguments["to_path"]],
                        "tool": "shortest_dependency_path"}
            return {"investigation_count": 0, "investigations": [],
                    "tool": "recall_investigations"}

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1"):
            result = asyncio.run(run_agent_loop(
                "show the path", "src/api/auth_routes.py", other_path="src/core/token_store.py"))

        display = result["result"]
        self.assertEqual(display["tool"], "shortest_dependency_path")
        self.assertEqual(display["hops"], 2)
        self.assertEqual(len(display["chain"]), 3)
        self.assertNotIn("affected_files", display)

    def test_agent_loop_preserves_bridge_display_result(self):
        from main import run_agent_loop

        async def fake_call_tool(name, arguments):
            if name == "find_bridge_modules_tool":
                return {"bridges": [{"path": "src/core/auth_service.py", "score": 12.5}],
                        "tool": "find_bridge_modules"}
            return {"investigation_count": 0, "investigations": [],
                    "tool": "recall_investigations"}

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.choose_analysis_tool",
                      return_value=("find_bridge_modules_tool", {}, "bridges", "rules")), \
                patch("main.record_investigation", return_value="inv-1"):
            result = asyncio.run(run_agent_loop(
                "show me the choke points", "src/core/token_store.py"))

        display = result["result"]
        self.assertEqual(display["tool"], "find_bridge_modules")
        self.assertEqual(display["bridges"][0]["path"], "src/core/auth_service.py")
        self.assertNotIn("affected_files", display)

    def test_agent_loop_recall_display_result_carries_tool_marker(self):
        # A recall result has no affected_files; the UI routes it to the memory
        # section via its tool marker instead of the standard trace render.
        from main import run_agent_loop

        async def fake_call_tool(name, arguments):
            self.assertEqual(name, "recall_investigations_tool")
            return {"target": "src/core/token_store.py", "investigation_count": 1,
                    "investigations": [{"summary": "earlier work"}],
                    "tool": "recall_investigations"}

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.choose_analysis_tool",
                      return_value=("recall_investigations_tool", {}, "recall", "rules")), \
                patch("main.record_investigation", return_value="inv-1"):
            result = asyncio.run(run_agent_loop(
                "what did we learn about this module before", "src/core/token_store.py"))

        display = result["result"]
        self.assertEqual(display["tool"], "recall_investigations")
        self.assertNotIn("affected_files", display)
        self.assertEqual(display["investigation_count"], 1)

    def test_agent_loop_respects_max_steps(self):
        from main import run_agent_loop

        calls = []

        async def fake_call_tool(name, arguments):
            calls.append(name)
            return {"impact_count": 1, "recommended_tests": [], "change_surface_score": 10,
                    "investigation_count": 0, "investigations": [], "ranked_tests": []}

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1"):
            result = asyncio.run(run_agent_loop(
                "rank and save a plan please", "src/core/token_store.py", max_steps=2))

        self.assertLessEqual(len(result["steps"]), 2)
        self.assertEqual(calls, ["trace_change_impact_tool", "rank_tests_pagerank_tool"])

    def test_targets_endpoint_reads_from_graph_in_demo_mode(self):
        from main import targets

        with patch("main.list_targets", return_value=[
            {"path": "src/core/token_store.py", "summary": "Token persistence adapter", "criticality": "high"}
        ]) as mock_targets:
            result = asyncio.run(targets())

        mock_targets.assert_called_once_with()
        self.assertEqual(result[0]["path"], "src/core/token_store.py")

    def test_targets_endpoint_reports_unreachable_graph(self):
        from main import targets
        from fastapi import HTTPException

        with patch("main.list_targets", side_effect=RuntimeError("FalkorDB is not configured.")):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(targets())
        self.assertEqual(ctx.exception.status_code, 503)

    def test_startup_demo_seed_is_best_effort(self):
        from main import seed_demo_graph_at_startup

        with patch("main.graph_status", return_value={"configured": False}), \
                patch("main.seed_demo") as mock_seed:
            seed_demo_graph_at_startup()
        mock_seed.assert_not_called()

        with patch("main.graph_status", return_value={"configured": True}), \
                patch("main.get_graph") as mock_graph, \
                patch("main.seed_demo") as mock_seed:
            seed_demo_graph_at_startup()
        mock_seed.assert_called_once_with(mock_graph.return_value)

    def test_plan_proposal_lifecycle(self):
        from graph_agent import (_approve_plan_proposal, _consume_plan_approval,
                                 _issue_plan_approval, PlanApprovalError)

        token = _issue_plan_approval("src/core/token_store.py", ["tests/test_a.py"])
        # a merely proposed token cannot be consumed by the write tool
        with self.assertRaises(PlanApprovalError):
            _consume_plan_approval(token, "src/core/token_store.py")
        # approval flips it to usable...
        record = _approve_plan_proposal(token, "src/core/token_store.py")
        self.assertEqual(record["tool"], "create_test_plan_tool")
        self.assertEqual(record["tests"], ["tests/test_a.py"])
        self.assertEqual(record["status"], "approved")
        # ...and consumption is single-use
        _consume_plan_approval(token, "src/core/token_store.py")
        with self.assertRaises(PlanApprovalError):
            _consume_plan_approval(token, "src/core/token_store.py")

    def test_plan_approval_is_idempotent(self):
        from graph_agent import _approve_plan_proposal, _issue_plan_approval

        token = _issue_plan_approval("src/core/token_store.py", [])
        _approve_plan_proposal(token, "src/core/token_store.py")
        # retrying approval (e.g. double-click) is harmless
        _approve_plan_proposal(token, "src/core/token_store.py")

    def test_plan_approval_rejects_unknown_token(self):
        from graph_agent import _consume_plan_approval, PlanApprovalError

        with self.assertRaises(PlanApprovalError):
            _consume_plan_approval("not-a-real-token", "src/core/token_store.py")

    def test_plan_approval_rejects_mismatched_target(self):
        from graph_agent import _approve_plan_proposal, _issue_plan_approval, PlanApprovalError

        token = _issue_plan_approval("src/core/token_store.py", [])
        with self.assertRaises(PlanApprovalError):
            _approve_plan_proposal(token, "src/core/other.py")

    def test_plan_approval_rejects_expired_token(self):
        from graph_agent import _approve_plan_proposal, _issue_plan_approval, PlanApprovalError

        token = _issue_plan_approval("src/core/token_store.py", [])
        with patch("graph_agent.time.time", return_value=1e12):
            with self.assertRaises(PlanApprovalError):
                _approve_plan_proposal(token, "src/core/token_store.py")

    def test_test_plan_endpoint_requires_proposal_token(self):
        from fastapi import HTTPException
        from main import PlanRequest, test_plan

        async def fail_if_called(name, arguments):
            raise AssertionError("write tool must not run without approval")

        with patch("main.call_mcp_tool", side_effect=fail_if_called):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(test_plan(PlanRequest(target_path="src/core/token_store.py")))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_test_plan_endpoint_approves_and_executes(self):
        from fastapi import HTTPException
        from graph_agent import _consume_plan_approval, _issue_plan_approval
        from main import PlanRequest, test_plan

        token = _issue_plan_approval("src/core/token_store.py", ["tests/test_a.py"])

        def fake_tool(name, arguments):
            # behave like the real MCP tool: consume the approved token exactly once
            _consume_plan_approval(arguments["proposal_token"], arguments["target_path"])
            return {"saved": True, "plan_id": "abc"}

        with patch("main.call_mcp_tool", side_effect=fake_tool) as mock_tool:
            result = asyncio.run(test_plan(PlanRequest(
                target_path="src/core/token_store.py", proposal_token=token)))
        mock_tool.assert_called_once_with(
            "create_test_plan_tool",
            {"target_path": "src/core/token_store.py", "rationale": "Change impact review",
             "proposal_token": token})
        self.assertTrue(result["saved"])
        # the proposal was consumed: a second save is refused without calling the tool
        with patch("main.call_mcp_tool") as mock_tool2:
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(test_plan(PlanRequest(
                    target_path="src/core/token_store.py", proposal_token=token)))
        self.assertEqual(ctx.exception.status_code, 403)
        mock_tool2.assert_not_called()

    def test_agent_loop_asks_for_second_module_on_path_request(self):
        from main import run_agent_loop

        async def fail_if_called(name, arguments):
            raise AssertionError(f"no tool should run without both endpoints: {name}")

        with patch("main.call_mcp_tool", side_effect=fail_if_called):
            result = asyncio.run(run_agent_loop(
                "show me the shortest path from this module", "src/core/token_store.py"))

        self.assertEqual(result["needs_input"], "destination_module")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["action"], "awaiting_input")
        self.assertIsNone(result["result"])
        self.assertIsNone(result["proposed_action"])
        self.assertIn("PATH TO", result["final_answer"])
        self.assertIn("src/core/token_store.py", result["final_answer"])

    def test_agent_loop_proposal_carries_proposal_token(self):
        from main import run_agent_loop

        async def fake_call_tool(name, arguments):
            if name == "trace_change_impact_tool":
                return {"impact_count": 1,
                        "recommended_tests": [{"path": "tests/test_a.py"}],
                        "change_surface_score": 10}
            if name == "find_impact_communities_tool":
                return {"target_community": 1, "community_count": 2, "impacted_communities": 1,
                        "communities": [], "tool": "find_impact_communities"}
            if name == "recall_investigations_tool":
                return {"investigation_count": 0, "investigations": []}
            raise AssertionError(f"unexpected tool call: {name}")

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1"):
            result = asyncio.run(run_agent_loop(
                "investigate and save a plan", "src/core/token_store.py"))

        token = result["proposed_action"]["proposal_token"]
        self.assertTrue(token)
        # the exact hole from review: a proposal token grabbed here must NOT
        # authorize a direct MCP write before the user approves it
        with self.assertRaises(graph_agent.PlanApprovalError):
            graph_agent.create_test_plan_tool(
                "src/core/token_store.py", "x", proposal_token=token)
        # after approval it works exactly once
        graph_agent._approve_plan_proposal(token, "src/core/token_store.py")
        with patch.object(graph_agent, "create_test_plan",
                          return_value={"saved": True, "plan_id": "p1"}):
            graph_agent.create_test_plan_tool(
                "src/core/token_store.py", "x", proposal_token=token)
        with self.assertRaises(graph_agent.PlanApprovalError):
            graph_agent.create_test_plan_tool(
                "src/core/token_store.py", "x", proposal_token=token)

    def test_run_evaluation_refused_for_local_dataset(self):
        with patch.object(graph_agent, "_ACTIVE_DATASET", "local"), \
                patch.object(graph_agent, "_ACTIVE_REPOSITORY_NAME", "My Repo"):
            report = graph_agent.run_evaluation()

        self.assertFalse(report["evaluated"])
        self.assertEqual(report["cases"], [])
        self.assertEqual(report["dataset"], "local")
        self.assertIn("demo", report["reason"].lower())

    def test_run_evaluation_marks_demo_reports_evaluated(self):
        with patch.object(graph_agent, "_ACTIVE_DATASET", "demo"), \
                patch.object(graph_agent, "trace_change_impact",
                             side_effect=ValueError("no graph in unit test")), \
                patch.object(graph_agent, "rank_tests_by_pagerank",
                             side_effect=RuntimeError("no graph in unit test")):
            report = graph_agent.run_evaluation()

        self.assertTrue(report["evaluated"])


    def test_create_test_plan_tool_refuses_without_approval(self):
        # Direct MCP-level call with no token must fail before any write.
        with self.assertRaises(graph_agent.PlanApprovalError):
            graph_agent.create_test_plan_tool("src/core/token_store.py")

    def test_create_test_plan_tool_accepts_approved_proposal(self):
        token = graph_agent._issue_plan_approval("src/core/token_store.py", ["tests/test_a.py"])
        graph_agent._approve_plan_proposal(token, "src/core/token_store.py")
        with patch.object(graph_agent, "create_test_plan",
                          return_value={"saved": True, "plan_id": "abc"}) as mock_write:
            result = graph_agent.create_test_plan_tool(
                "src/core/token_store.py", "Review", proposal_token=token)
        mock_write.assert_called_once_with("src/core/token_store.py", "Review",
                                           approved_tests=["tests/test_a.py"],
                                           test_order="pagerank")
        self.assertTrue(result["saved"])
        # the token was consumed: a replay is refused
        with self.assertRaises(graph_agent.PlanApprovalError):
            graph_agent.create_test_plan_tool(
                "src/core/token_store.py", "Review", proposal_token=token)

    def test_investigate_asks_for_destination_on_path_prompt(self):
        from main import InvestigateRequest, investigate

        async def fail_if_called(name, arguments):
            raise AssertionError(f"tool must not run without both endpoints: {name}")

        with patch("main.choose_analysis_tool",
                   return_value=("shortest_dependency_path_tool", None,
                                 "path requested", "rules")), \
                patch("main.call_mcp_tool", side_effect=fail_if_called):
            response = asyncio.run(investigate(InvestigateRequest(
                prompt="show me the shortest path", target_path="src/core/token_store.py")))

        self.assertEqual(response["needs_input"], "destination_module")
        self.assertIsNone(response["result"])
        self.assertNotIn("plan_proposal_token", response)
        self.assertIn("PATH TO", response["final_answer"])

    def test_investigate_passes_other_path_to_shortest_path_tool(self):
        from main import InvestigateRequest, investigate

        seen = {}

        async def fake_call_tool(name, arguments):
            seen.update(arguments)
            return {"connected": True, "hops": 1, "from": "a", "to": "b", "chain": ["a", "b"]}

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1"):
            response = asyncio.run(investigate(InvestigateRequest(
                prompt="trace this", target_path="src/a.py", other_path="src/b.py")))

        self.assertEqual(response["selected_tool"], "shortest_dependency_path_tool")
        self.assertEqual(seen, {"from_path": "src/a.py", "to_path": "src/b.py"})


    def test_investigate_records_bridge_investigation(self):
        # Regression: the investigate memory allowlist omitted
        # find_bridge_modules_tool, so bridge findings were never remembered.
        from main import InvestigateRequest, investigate

        async def fake_call_tool(name, arguments):
            self.assertEqual(name, "find_bridge_modules_tool")
            return {"bridges": [{"path": "src/core/auth_service.py",
                                 "betweenness_score": 12.5}],
                    "tool": "find_bridge_modules"}

        with patch("main.choose_analysis_tool",
                   return_value=("find_bridge_modules_tool", {}, "choke points", "rules")), \
                patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1") as mock_record, \
                patch("main.recall_investigations",
                      return_value={"investigation_count": 0, "investigations": []}):
            response = asyncio.run(investigate(InvestigateRequest(
                prompt="show me the choke points", target_path="src/core/token_store.py")))

        self.assertEqual(response["selected_tool"], "find_bridge_modules_tool")
        mock_record.assert_called_once()
        summary = mock_record.call_args[0][2]
        self.assertIn("auth_service", summary["text"])

    def test_approved_plan_persists_reviewed_test_list(self):
        # The approval gate must save exactly what the user reviewed: the
        # token stores the approved test list, and the save must persist it
        # verbatim even if the graph would now rank differently.
        graph = FakeGraph(
            path_rows=[("src/core/auth_service.py", "high", 1,
                        ["src/core/auth_service.py", "src/core/token_store.py"])],
            test_rows=[("tests/test_auth_service.py", ["src/core/token_store.py"])],
        )
        with patch.object(graph_agent, "get_graph", return_value=graph):
            token = graph_agent._issue_plan_approval(
                "src/core/token_store.py", ["tests/test_reviewed.py"])
            graph_agent._approve_plan_proposal(token, "src/core/token_store.py")
            result = graph_agent.create_test_plan_tool(
                "src/core/token_store.py", "review", token)
        # the graph would recompute tests/test_auth_service.py, but the
        # approved snapshot (tests/test_reviewed.py) is what gets saved
        self.assertEqual(result["tests_to_run"], ["tests/test_reviewed.py"])
        _, params = graph.writes[-1]
        self.assertEqual(params["test_paths"], ["tests/test_reviewed.py"])

    def test_investigate_issues_no_plan_token_without_tests(self):
        # A bridge result carries no test list: no approval token may be
        # issued, so the save path cannot persist an empty plan.
        from main import InvestigateRequest, investigate

        async def fake_call_tool(name, arguments):
            return {"bridges": [{"path": "src/core/auth_service.py",
                                 "betweenness_score": 12.5}],
                    "tool": "find_bridge_modules"}

        with patch("main.choose_analysis_tool",
                   return_value=("find_bridge_modules_tool", {}, "choke points", "rules")), \
                patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1"), \
                patch("main.recall_investigations",
                      return_value={"investigation_count": 0, "investigations": []}):
            response = asyncio.run(investigate(InvestigateRequest(
                prompt="show me the choke points", target_path="src/core/token_store.py")))

        self.assertNotIn("plan_proposal_token", response)

    def test_plan_preserves_trace_ordering_label(self):
        # A trace-only proposal was never PageRank-ranked: the saved plan
        # must report its actual ordering, not "pagerank".
        graph = FakeGraph(
            path_rows=[("src/core/auth_service.py", "high", 1,
                        ["src/core/auth_service.py", "src/core/token_store.py"])],
            test_rows=[("tests/test_auth_service.py", ["src/core/token_store.py"])],
        )
        with patch.object(graph_agent, "get_graph", return_value=graph):
            token = graph_agent._issue_plan_approval(
                "src/core/token_store.py", ["tests/test_auth_service.py"],
                test_order="trace")
            graph_agent._approve_plan_proposal(token, "src/core/token_store.py")
            result = graph_agent.create_test_plan_tool(
                "src/core/token_store.py", "review", token)
        self.assertEqual(result["test_order"], "trace")
        self.assertEqual(result["tests_to_run"], ["tests/test_auth_service.py"])
        self.assertIn("trace order", result["action"])
        self.assertNotIn("PageRank", result["action"])

    def test_agent_loop_proposes_no_plan_without_tests(self):
        # wants_plan with an empty test list: no proposal, no token, so the
        # save button stays disabled instead of offering an empty plan.
        from main import run_agent_loop

        async def fake_call_tool(name, arguments):
            if name == "trace_change_impact_tool":
                return {"target": "src/core/token_store.py", "impact_count": 0,
                        "affected_files": [], "recommended_tests": [],
                        "tool": "trace_change_impact"}
            if name == "find_impact_communities_tool":
                return {"target": "src/core/token_store.py", "impacted_communities": 0,
                        "tool": "find_impact_communities"}
            if name == "recall_investigations_tool":
                return {"investigation_count": 0, "investigations": []}
            raise AssertionError(f"unexpected tool call: {name}")

        with patch("main.call_mcp_tool", side_effect=fake_call_tool), \
                patch("main.record_investigation", return_value="inv-1"):
            result = asyncio.run(run_agent_loop(
                "Investigate the change and save a plan", "src/core/token_store.py"))

        self.assertIsNone(result["proposed_action"])
        self.assertNotIn("proposed:create_test_plan_tool",
                         [step["action"] for step in result["steps"]])

    def test_mcp_server_creation_emits_no_incomplete_field_warning(self):
        # Regression: pydantic-settings emitted IncompleteFieldDefinitionWarning
        # about FastMCP's own `lifespan` field when the server was created.
        # graph_agent installs a targeted ignore-filter for that category, so
        # promoting it to an error in a fresh interpreter must not fail.
        project_dir = str(Path(__file__).resolve().parent.parent)
        code = (
            "import sys, warnings; "
            f"sys.path.insert(0, {project_dir!r}); "
            "from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning; "
            "warnings.simplefilter('error', IncompleteFieldDefinitionWarning); "
            "import graph_agent; "
            # the filter graph_agent installs must hold even when the
            # category is promoted to an error...
            "warnings.warn('synthetic', IncompleteFieldDefinitionWarning); "
            "print('server created cleanly')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=project_dir, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])
        self.assertIn("server created cleanly", proc.stdout)


if __name__ == "__main__":
    unittest.main()
