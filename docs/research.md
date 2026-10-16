# Graph Hacks Research Notes

Researched October 15, 2026. Recheck the organizer pages before submitting; the rules explicitly reserve the right to update event details.

## Executive read

Wry is a strong, narrow fit for Track 1: it investigates a proposed code change by traversing a dependency graph, finds tests connected to the impacted surface, explains the paths, and can save a graph-derived test plan after a separate user action. The most important product proof is not a polished graph visualization; it is a live, repeatable case where removing FalkorDB removes the dependency reasoning and the resulting action.

The safest demo dataset is Wry's synthetic Northstar Commerce graph. The optional local indexer is now disabled by default and guarded in both the API and indexer. Enable it only for a public or otherwise non-restricted repository whose metadata is allowed in the graph. Do not index the user's private Wry Agent checkout for the hackathon demo.

## Official challenge and rules

The official [Graph Hacks overview](https://www.wemakedevs.org/hackathons/falkordb) describes three tracks. Track 1, **Agents That Act on Connected Data**, emphasizes multi-hop reasoning, graph queries/algorithms as agent tools, MCP tool choice, action grounded in graph evidence, and explainable results. Its examples include codebase agents, which makes Wry's code-change impact investigation directly relevant.

The [official rules](https://www.wemakedevs.org/hackathons/falkordb/rules) currently state:

- FalkorDB must be the primary graph database and central to product behavior; a removal test should make Wry's impact analysis and graph-backed plan unavailable.
- Allowed data is public, synthetic, or the participant's own, but private, login-protected, paywalled, personal, and otherwise restricted information must not be loaded into the graph. Treat the restriction as controlling: ownership alone is not permission to put private data into the competition graph.
- Main coding and design work must start after the event opens (October 15, 12:01 AM IST); earlier ideas, notes, graph sketches, and diagrams are allowed. Keep the work chronology honest.
- Submission requires a public source repository; README setup and graph model; dependent Cypher or graph algorithms; demo video; live deployment or complete local instructions; and named track(s).
- AI coding tools are permitted with disclosure, but the participant must understand and explain the implementation. AI-only output without meaningful contribution and verification may be rejected.
- The event page names an iPhone 18 Pro for the Track 1 winner. The public pages do not publish numeric judging weights, so do not invent a scoring rubric or claim a win is predictable.
- The listed event window is October 15, 12:01 AM IST through October 18, 11:59 PM IST. Confirm the submission form and announcements for any changes.

## Fit and differentiation

The project has good structural fit: `IMPORTS` supplies multi-hop paths, `COVERS` ties affected modules to tests, and MCP exposes graph operations as tools. FalkorDB is not merely storing display data: the server queries it for the target, transitive importers, criticality, and test recommendations. `TestPlan` persistence makes a graph-derived proposal an explicit action.

Oct 15 update: the Track 1 key focus list on the official event page names "graph queries and algorithms as agent tools" (shortest path, PageRank, centrality, connected components, community detection). Wry now covers this with `rank_tests_pagerank_tool`, an MCP tool that runs FalkorDB's `algo.pageRank('File', 'IMPORTS')` and orders recommended tests by the PageRank importance of the impacted modules they cover (riskiest first). The algorithm call is a parameterized, read-only procedure; scores are relative (sum to 1.0) and presented as importance ordering, not defect probability. Follow-up: the PageRank ranking has its own expected-results evaluation case, saved test plans order tests by PageRank importance, and the UI renders an impact map of the dependency paths.

Oct 15 round 5 (agent upgrade): Wry is now a real agent loop, not a single-shot router. `POST /api/agent/run` runs a bounded Thought → Action → Observation loop (max 5 steps) chaining trace → PageRank → CDLP communities → memory recall, narrating each step's decision rule; the write tool is proposed, never auto-executed. Agent memory is graph-native: investigations recorded as `Investigation` nodes, recalled via `recall_investigations_tool`. Two more algorithms as tools: `algo.betweenness` (bridge modules) and shortest-path between two modules. Per-query latency timings in every result. This closes the "agent that acts on connected data" gaps: multi-step agency, memory across sessions ("remembers what it learns" from the event brief), and plural algorithms-as-tools.

The main risk is that a code-impact assistant can look like a static dependency viewer unless the demo shows useful decisions and validation. Prioritize a compact evaluation set with known expected importers/tests, then show a change to a central module and a leaf module with visibly different impact. Report misses as well as correct paths. State that `COVERS` currently represents statically observed direct test imports, not runtime coverage or proof a test will catch a regression.

Oct 15 round 10 (blast-radius communities): added `find_impact_communities_tool` — FalkorDB's `algo.labelPropagation` (CDLP) over the import graph, reporting the target's community and how many distinct communities the transitive impact touches. The agent loop is now 5 steps (trace → PageRank rank → CDLP communities → memory recall → proposed plan) with the communities analysis, Cypher, and timings merged into the display result; a seventh evaluation case checks CDLP grouping structure (ids are non-deterministic, so only structure is asserted); the UI renders a BLAST RADIUS section. README hardened as a judged artifact: kill-test statement, why-not-a-vector-DB rationale, Limitations section, one-line "why this algorithm" notes. Deliberately skipped: full-text module search (the UI's substring filter already covers discovery; it adds no algorithm story) and Yen's k-shortest paths (pending verification that FalkorDB Cloud exposes the procedure).

The current surface score is a transparent breadth heuristic, not a defect probability. Keep that caveat visible in the README/demo; don't present the percentage as risk calibration.

## Privacy and data handling decision

Graph Hacks rules prohibit private/restricted graph data. A local-only parser does not by itself solve this if names and dependency metadata are sent to Cloud. Wry therefore defaults to the synthetic graph. Repository metadata transfer requires all of the following:

1. `WRY_ALLOW_REPOSITORY_METADATA_UPLOAD=true` is deliberately set by the operator.
2. The UI repeats the repository target, metadata fields, and public/non-restricted eligibility assertion before sending.
3. The API and indexing function each enforce the opt-in; the browser is not a security boundary.
4. Only relative Python paths, heuristic labels, and static import/test edges are uploaded; source contents are parsed locally and never sent.

This gate is a guardrail, not an automated legal/privacy classifier. The operator remains responsible for selecting eligible data. `.env` credentials must stay out of Git, screenshots, prompts, and demo footage. Use synthetic Northstar by default for the event submission.

## Architecture and engineering evidence

- The Python MCP server uses the v1 `FastMCP` API, mounted by FastAPI over Streamable HTTP. The app chooses only from a fixed read-only tool allowlist for investigation; model text cannot become Cypher.
- Cypher values are passed as parameters. Import traversal is bounded to five hops. Repository IDs scope file nodes and graph relationships so one repository's paths do not bleed into another's query.
- The graph's importer-to-dependency edge direction is intentional; query results reverse the path for the human narrative.
- Demo seeding is idempotent and batched. Local metadata parsing uses Python AST and bounded file counts/sizes, skips hidden/generated/virtualenv folders and symlinks, and stores no source text.
- The test suite uses a fake graph for deterministic behavior and temporary local source fixtures. It does not validate all Cloud-specific Cypher/runtime behavior. A live Cloud smoke test was run October 15, 2026 against the synthetic graph: changing `src/core/token_store.py` returned four transitive importers, five recommended tests, and a 62% surface score across eight modules. Repeat and capture the query in the final demo.

## MCP SDK compatibility research

The official [Python SDK repository](https://github.com/modelcontextprotocol/python-sdk) currently describes v2 as the stable line. Installing unbounded `mcp` now selects v2. The SDK's [migration guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md) documents breaking changes including `FastMCP` becoming `MCPServer` and module paths moving from `mcp.server.fastmcp` to `mcp.server.mcpserver`. Wry currently uses the v1 API, so `mcp>=1.29,<2` is a deliberate compatibility pin, not a claim that v1 is newest. The SDK says v1 continues to receive critical fixes; schedule a v2 migration only with transport and tool integration tests.

The official [tool docs](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/servers/tools.md) explain that type hints provide input schemas and descriptions come from tool docstrings. Tool annotations such as read-only or destructive are behavioral hints, not security controls. Wry marks investigations read-only and the proposed-plan action as a non-idempotent write; it still enforces allowlists and authorization in application code.

## FalkorDB implementation research

The [official FalkorDB Python client](https://github.com/FalkorDB/falkordb-py) demonstrates `Graph.query()` for graph operations and `Graph.ro_query()` for read-only queries. FalkorDB's [Cypher parameter documentation](https://falkordb.github.io/docs-staging/cypher/cypher-support/) describes parameters, supporting Wry's choice to bind values instead of interpolating user input. The SDK/client docs and syntax should be rechecked against the exact installed versions before adding procedures or algorithms.

FalkorDB's [Hack for Humanity project review](https://www.falkordb.com/blog/how-builders-at-hack-for-humanity-used-falkordb-to-solve-real-community-problems/) is useful sponsor-authored engineering context, not the Graph Hacks rubric. Transferable patterns include grounding a real task in connected data, provenance, bounded/read-only query paths, idempotent loading, and validating behavior beyond a screenshot.

## Remaining work, in order

1. ~~Add a small expected-results evaluation table: target, expected multi-hop importers, expected recommended tests, actual result, and pass/fail. Include a case with no importer and no covering test.~~ Done Oct 15: `run_evaluation()` in `graph_agent.py` plus `GET /api/agent/evaluate`; five cases (four change targets with expected importer/test sets, one unknown-target rejection case) with per-case diffs and a pass/total summary.
2. Improve test relationships beyond import heuristics if time permits. For Python, AST import edges are not runtime coverage and may miss dynamic imports, namespace/package edge cases, and aliases; label them as static evidence.
3. Test two repositories with overlapping relative paths to verify repository scoping end to end, including test-plan writes.
4. Publish the repo, record a concise demo from the synthetic dataset, and complete the organizer's submission checklist.
5. Participant reviews and understands every change, adds an accurate AI-assistance disclosure, and can explain one Cypher traversal and its limitations without relying on the generated UI narrative.

## Submission checklist

- Track named: Agents That Act on Connected Data.
- Public source repository, README setup, graph schema/data model, and core Cypher documented.
- Demo video shows a real live query, multi-hop paths, test recommendations, tool selection, provenance, and an explicit plan-save action.
- Live deployment or reproducible local setup instructions; synthetic graph available by default.
- No private or restricted graph data, and no secrets in public artifacts.
- AI-tool use disclosed; project reviewed and explainable by the participant.
- Current event deadline and submission instructions rechecked on the official page.
