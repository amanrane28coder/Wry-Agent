# Submission Checklist

Official checklist: [Graph Hacks rules](https://www.wemakedevs.org/hackathons/falkordb/rules). Re-check before submission in case the organizers update the rules.

## In this working copy

- [x] Track named: Agents That Act on Connected Data.
- [x] README includes setup, graph model, Cypher, and local run instructions.
- [x] Synthetic default graph and distinct local-index provenance; the local index is opt-in and sends metadata only.
- [x] MCP tools for module discovery, multi-hop impact, and proposed test-plan persistence.
- [x] Separate human confirmation before the graph write action.
- [x] Regression tests for traversal shaping, unknown targets, idempotent seed, tool allowlisting, and plan persistence.
- [x] Research notes and demo script.
- [x] `/api/targets` reads the module list from FalkorDB in every mode; the demo seed runs once at startup so the MCP investigation tools stay genuinely read-only.
- [x] Expected-results evaluation (`run_evaluation`, `GET /api/agent/evaluate`) with per-case diffs and an unknown-target rejection case.
- [x] AI assistance disclosure drafted in the README (participant must verify accuracy before submitting).
- [x] Investigation results include the exact executed Cypher, shown in the UI under GRAPH QUERY.
- [x] RUN EVALUATION panel in the web UI renders the expected-results report with per-case diffs.
- [x] Named graph algorithm as an MCP tool: `rank_tests_pagerank_tool` runs FalkorDB `algo.pageRank` over the import graph; UI shows RISKIEST FIRST ranking.
- [x] PageRank expected-results case in RUN EVALUATION (top-ranked test must cover the highest-PageRank impacted module; scores descending).
- [x] Saved test plans order tests by PageRank importance (`test_order: pagerank` in the plan response).
- [x] IMPACT MAP visualization of dependency paths (target → importers by hop, change-propagation edges) in the results UI.
- [x] Specific AI assistance disclosure in the README (grouped two-section summary, participant-centered); participant must verify accuracy and add any AI tools they used themselves.
- [x] Agent loop: bounded Thought → Action → Observation loop (`POST /api/agent/run`) chaining MCP tools with a visible AGENT TRACE timeline; write action proposed, never auto-executed (approval card).
- [x] Agent memory: investigations recorded as `Investigation` nodes in FalkorDB; `recall_investigations_tool` reads history back; AGENT MEMORY section in UI.
- [x] Two more graph algorithms as MCP tools: `find_bridge_modules_tool` (`algo.betweenness`) and `shortest_dependency_path_tool` (shortest directed IMPORTS chain), wired through router + model allowlist, surfaced in UI.
- [x] Per-query latency timings in every result, shown in the GRAPH QUERY panel.

## Still required from the participant

- [x] Connect an authorized FalkorDB Cloud instance and run a live end-to-end impact trace.
- [x] Save a proposed test plan and verify the saved graph action in FalkorDB Browser (verified: 7 TestPlan nodes; latest targets src/core/token_store.py with REVIEW + RUN edges).
- [x] Create a public source repository and confirm `.env` is not tracked (https://github.com/amanrane28coder/Wry-Agent; sensitive-info sweep clean, real `.env` gitignored).
- [x] Record and review a demo video showing real FalkorDB responses, MCP selection, explainability, and the approval-gated write (final 2:26 video with the participant's own voiceover, approved).
- [ ] Add an accurate disclosure of AI coding tools used and make sure you can explain the full implementation.
- [ ] Final submission in the official WeMakeDevs event panel before the deadline (registration confirmed: solo, Track 1).

Do not mark an item complete based only on a unit test or a mocked graph response.
