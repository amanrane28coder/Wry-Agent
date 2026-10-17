# Demo Script (About 2½ Minutes)

Keep the FalkorDB connection status visible. The synthetic demo is the no-upload default. If model selection is enabled, use a synthetic question and make clear that the prompt is sent to the configured model endpoint; otherwise show the deterministic local mode.

## 0:00–0:20 | Problem

“A code change can break files several imports away. Wry is an agent that investigates the change in FalkorDB, chains graph tools to decide what matters, remembers what it checked, and proposes a test plan — with every step visible.”

Show the `Northstar Commerce` synthetic graph label and the selected `src/core/token_store.py` module.

## 0:20–0:55 | The agent runs a loop (the wow)

Type: “Investigate this change, rank the riskiest tests, and save a plan.” Click **RUN AGENT**. Walk the **AGENT TRACE** timeline: step 1, the agent thinks (“the request concerns a change…”) and calls `trace_change_impact_tool`; step 2, it reads the observation (4 importers, 5 tests) and calls `rank_tests_pagerank_tool`; step 3, it groups the blast radius into architectural communities with `find_impact_communities_tool` (CDLP); step 4, it consults memory via `recall_investigations_tool`; step 5, it *proposes* `create_test_plan_tool` but does not execute it — the card reads “nothing is written until you approve it.” This is the Thought → Action → Observation loop, bounded at 5 steps, with the write action approval-gated. No keyboard magic: the loop selects each tool itself through its policy - the first tool is rules-routed from the prompt, then a fixed follow-up sequence (trace -> rank -> communities -> recall -> propose), with optional model-assisted selection.

## 0:55–1:25 | Graph does the reasoning

Walk the **IMPACT MAP** from the changed token store outward: target on the left, importers fanning right by hop distance, edges showing the direction the change propagates. Show the importer count, repository surface percentage, and the **RISKIEST FIRST** ranking — FalkorDB's `algo.pageRank` scoring each module by how much of the repository depends on it. Expand **GRAPH QUERY** to show the exact Cypher executed, with per-query millisecond timings — the removal-test evidence: no graph, no paths, and the graph answers in milliseconds.

## 1:25–1:45 | The agent remembers

Run the agent a second time on the same module (or point at the **AGENT MEMORY** section): “The agent remembers 1 past investigation — 4 importers, 5 tests, 2 minutes ago.” Investigations are stored as `Investigation` nodes in FalkorDB itself and recalled with a Cypher query, so the agent learns across sessions instead of re-deriving everything. Optionally show **CRITICAL BRIDGES** (`algo.betweenness` choke points) or the shortest-path view via the “PATH TO” selector.

## 1:45–2:00 | Human approves the action

Click **APPROVE & SAVE PLAN** on the proposed action card. Show the success receipt: Wry wrote a `TestPlan` node plus `REVIEW` and `RUN` edges to FalkorDB, tests ordered by PageRank importance. It does not edit files or execute tests. In FalkorDB Browser, optionally show the saved node connected to the impacted files and tests.

## 2:00–2:15 | Failure handling (the judge beat)

Select an unknown module (or type a bad target) and run: the agent fails closed with “not in the indexed repository” — no fabricated impact data. Then run **RUN EVALUATION**: seven expected-results cases pass against the live graph, including the unknown-target rejection, the PageRank ranking case, and the CDLP community case. Judges probe failure modes; this is the 15-second answer.

## 2:15–2:25 | Why FalkorDB matters

“Remove FalkorDB and Wry cannot trace dependencies, rank tests, recall past investigations, or persist this plan. The graph is the agent's working memory and decision engine, not a visualization layer.”

Do not imply benchmark performance, real-world repository coverage, or automated test execution unless separately measured and demonstrated.
