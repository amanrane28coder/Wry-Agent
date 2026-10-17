"""Local web app that exposes Wry's FalkorDB tools over MCP."""

import json
import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

from graph_agent import active_repository_info, get_graph, graph_status, index_local_repository, list_targets, local_repository_name, mcp, recall_investigations, record_investigation, repository_index_enabled, run_evaluation, seed_demo
from graph_agent import PlanApprovalError, _issue_plan_approval, _approve_plan_proposal

ROOT = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    async with mcp.session_manager.run():
        seed_demo_graph_at_startup()
        yield


def seed_demo_graph_at_startup():
    """Seed the synthetic demo graph once at startup.

    Seeding here (instead of inside the read tools) keeps the MCP
    investigation tools genuinely read-only, matching their annotations.
    Best-effort: if FalkorDB is unreachable at boot, the tools fail closed
    per request, so restart the app once the instance is reachable.
    """
    if not graph_status()["configured"]:
        return
    try:
        seed_demo(get_graph())
    except Exception as exc:
        print(f"Wry: demo graph seed skipped at startup ({exc}); restart the app once FalkorDB is reachable.")


mcp_http_app = mcp.streamable_http_app()
app = FastAPI(title="Wry Change Impact", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.mount("/mcp", mcp_http_app, name="wry-mcp")


class InvestigateRequest(BaseModel):
    prompt: str = Field(min_length=2, max_length=600)
    target_path: str
    other_path: str | None = None


class AgentRunRequest(BaseModel):
    prompt: str = Field(min_length=2, max_length=600)
    target_path: str
    other_path: str | None = None


class PlanRequest(BaseModel):
    target_path: str
    rationale: str = Field(default="Change impact review", max_length=500)
    proposal_token: str = Field(default="", max_length=128)


async def call_mcp_tool(name: str, arguments: dict[str, Any]):
    if not graph_status()["configured"]:
        raise HTTPException(
            status_code=503,
            detail="FalkorDB is not connected. Configure the instance in wry_hackathon/.env first.",
        )
    try:
        result = await mcp.call_tool(name, arguments)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"FalkorDB MCP tool unavailable: {exc}") from exc
    if isinstance(result, dict):
        return result.get("result", result)
    if result and getattr(result[0], "text", None):
        try:
            return json.loads(result[0].text)
        except json.JSONDecodeError:
            return {"message": result[0].text}
    return result


def select_analysis_tool(prompt: str):
    words = set(prompt.lower().replace("?", " ").replace("/", " ").split())
    if words.intersection({"shortest", "between"}):
        return "shortest_dependency_path_tool", None, "The request asks for the shortest dependency path; use the shortest-path query in FalkorDB."
    if words.intersection({"bridge", "bridges", "choke", "bottleneck", "bottlenecks", "betweenness"}):
        return "find_bridge_modules_tool", {}, "The request asks for architectural choke points; use betweenness centrality in FalkorDB."
    if words.intersection({"community", "communities", "blast", "cluster", "clusters", "neighborhood", "neighbourhood"}):
        return "find_impact_communities_tool", None, "The request asks about blast-radius communities; use CDLP community detection in FalkorDB."
    if words.intersection({"remember", "remembers", "memory", "history", "past", "previous", "previously", "before"}):
        return "recall_investigations_tool", None, "The request asks about past work; recall the agent's investigation history from FalkorDB."
    if words.intersection({"rank", "ranking", "ranked", "riskiest", "prioritize", "prioritise", "priority"}):
        return "rank_tests_pagerank_tool", None, "The request asks to rank or prioritize tests; use PageRank over the import graph in FalkorDB."
    if words.intersection({"list", "show", "available", "modules", "files", "targets"}):
        return "list_code_targets", {}, "The request asks for the available code targets."
    return "trace_change_impact_tool", None, "The request asks about a change; trace dependents and covering tests in FalkorDB."


def _model_selection_enabled():
    return os.getenv("LLM_TOOL_SELECTION", "false").lower() in {"1", "true", "yes"} and bool(os.getenv("OPENAI_API_KEY"))


async def choose_analysis_tool(prompt: str):
    fallback = select_analysis_tool(prompt)
    if not _model_selection_enabled():
        return (*fallback, "rules")

    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_code_targets",
                "description": "Use when the user wants to discover or browse indexed repository modules.",
                "strict": True,
                "parameters": {
                    "type": "object", "properties": {"reason": {"type": "string"}},
                    "required": ["reason"], "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "trace_change_impact_tool",
                "description": "Use for a proposed code change: traverse multi-hop import dependencies and find covered tests.",
                "strict": True,
                "parameters": {
                    "type": "object", "properties": {"reason": {"type": "string"}},
                    "required": ["reason"], "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "rank_tests_pagerank_tool",
                "description": "Use when the user wants tests ranked or prioritized: PageRank over the import graph orders the recommended tests, riskiest first.",
                "strict": True,
                "parameters": {
                    "type": "object", "properties": {"reason": {"type": "string"}},
                    "required": ["reason"], "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "shortest_dependency_path_tool",
                "description": "Use when the user asks for the shortest dependency path between two modules. Needs from_path and to_path; only offer when both modules are known.",
                "strict": True,
                "parameters": {
                    "type": "object", "properties": {"reason": {"type": "string"}},
                    "required": ["reason"], "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_bridge_modules_tool",
                "description": "Use when the user asks about architectural choke points, bottlenecks, or the most connector-critical modules: betweenness centrality over the import graph.",
                "strict": True,
                "parameters": {
                    "type": "object", "properties": {"reason": {"type": "string"}},
                    "required": ["reason"], "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_impact_communities_tool",
                "description": "Use when the user asks about blast-radius communities, clusters, or architectural neighborhoods: CDLP community detection over the import graph.",
                "strict": True,
                "parameters": {
                    "type": "object", "properties": {"reason": {"type": "string"}},
                    "required": ["reason"], "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recall_investigations_tool",
                "description": "Use when the user asks about past investigations, history, or what was checked before: recalls the agent's FalkorDB-backed memory.",
                "strict": True,
                "parameters": {
                    "type": "object", "properties": {"reason": {"type": "string"}},
                    "required": ["reason"], "additionalProperties": False,
                },
            },
        },
    ]
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "messages": [
            {"role": "system", "content": "Select exactly one read-only Wry MCP tool. Choose list_code_targets only to discover available modules; choose rank_tests_pagerank_tool when the request asks to rank or prioritize tests; choose find_bridge_modules_tool for choke points or bottlenecks; choose find_impact_communities_tool for blast-radius communities, clusters, or architectural neighborhoods; choose recall_investigations_tool for past investigations or history; choose shortest_dependency_path_tool only when the request names two modules and a path between them; otherwise choose trace_change_impact_tool to investigate impact. Do not propose writes or code execution. Give a concise reason in the tool arguments."},
            {"role": "user", "content": prompt[:600]},
        ],
        "tools": tools,
        "tool_choice": "required",
        "parallel_tool_calls": False,
        "temperature": 0,
    }
    try:
        response_data = await asyncio.to_thread(_request_tool_selection, base_url, payload)
        call = response_data["choices"][0]["message"]["tool_calls"][0]["function"]
        name = call["name"]
        arguments = json.loads(call["arguments"])
        if name not in {"list_code_targets", "trace_change_impact_tool", "rank_tests_pagerank_tool",
                         "shortest_dependency_path_tool", "find_bridge_modules_tool",
                         "find_impact_communities_tool", "recall_investigations_tool"}:
            raise ValueError("Model selected a tool outside the read-only allowlist.")
        reason = str(arguments.get("reason", "Model selected the read-only graph tool."))[:240]
        return name, {}, reason, "model"
    except (URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return (*fallback, "rules_fallback")


def _request_tool_selection(base_url: str, payload: dict):
    request = Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=12.0) as response:
        return json.loads(response.read())


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
async def status():
    mode = "Model-assisted MCP selection" if _model_selection_enabled() else "Rules-based MCP selection"
    return {
        **graph_status(), **active_repository_info(),
        "index_repository": local_repository_name(),
        "repository_index_enabled": repository_index_enabled(),
        "mcp_endpoint": "/mcp/", "agent_mode": mode,
    }


@app.get("/api/targets")
async def targets():
    # Always read from FalkorDB so the module list reflects the graph, even in
    # demo mode. A connection failure is a 503, not a silent empty list, so the
    # UI can tell "FalkorDB unreachable" apart from "no targets" and the status
    # indicator never claims "online" for a graph it cannot reach.
    try:
        return list_targets()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="FalkorDB is not reachable.") from exc


@app.post("/api/repository/index")
async def index_repository(request: Request):
    if request.headers.get("X-Wry-Action") != "index-local-repository":
        raise HTTPException(status_code=403, detail="Explicit local indexing confirmation is required.")
    if not repository_index_enabled():
        raise HTTPException(
            status_code=403,
            detail="Repository indexing is disabled. Enable it only for a public or otherwise non-restricted repository.",
        )
    try:
        return await asyncio.to_thread(index_local_repository)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not index repository metadata in FalkorDB.") from exc


@app.post("/api/agent/investigate")
async def investigate(request: InvestigateRequest):
    if request.other_path:
        tool_name, selection_reason, selection_mode = (
            "shortest_dependency_path_tool",
            "Two modules were selected; finding the shortest dependency path between them in FalkorDB.",
            "rules",
        )
        arguments = {"from_path": request.target_path, "to_path": request.other_path}
    else:
        tool_name, _, selection_reason, selection_mode = await choose_analysis_tool(request.prompt)
        arguments = {} if tool_name == "list_code_targets" else {"target_path": request.target_path}
    if tool_name == "shortest_dependency_path_tool" and not request.other_path:
        # A path needs two endpoints; ask for the destination instead of
        # calling the tool with a missing argument.
        return {
            "selected_tool": tool_name,
            "selection_reason": "The request asks for a shortest dependency path, but only one module was selected.",
            "selection_mode": selection_mode,
            "result": None,
            "needs_input": "destination_module",
            "final_answer": (f"To find the shortest dependency path from {request.target_path}, "
                             "select a destination module in PATH TO — or name the second module."),
        }
    result = await call_mcp_tool(tool_name, arguments)
    if selection_mode == "rules_fallback" and _model_selection_enabled():
        selection_reason = f"Model selector was unavailable; deterministic rule fallback. {selection_reason}"
    response = {
        "selected_tool": tool_name,
        "selection_reason": selection_reason,
        "selection_mode": selection_mode,
        "result": result,
    }
    # Agent memory: remember what was learned. The write happens here in the
    # agent runtime — the MCP investigation tools themselves stay read-only.
    if not isinstance(result, list) and tool_name in {
        "trace_change_impact_tool", "rank_tests_pagerank_tool", "shortest_dependency_path_tool",
        "find_impact_communities_tool",
    }:
        memory = _summarize_for_memory(tool_name, result)
        try:
            record_investigation(request.target_path, tool_name, memory)
        except Exception:
            pass  # memory is best-effort; never fail an investigation over it
        try:
            response["past_investigations"] = recall_investigations(request.target_path)
        except Exception:
            response["past_investigations"] = {"investigation_count": 0, "investigations": []}
    if not isinstance(result, list):
        # Issue a server-side approval for the plan the SAVE TEST PLAN button
        # would write. The write endpoint only executes with this token.
        proposed_tests = []
        if isinstance(result, dict):
            ranked = result.get("ranked_tests") or []
            traced = result.get("recommended_tests") or []
            proposed_tests = [t["path"] for t in ranked] or [t["path"] for t in traced]
        response["plan_proposal_token"] = _issue_plan_approval(request.target_path, proposed_tests)
    return response


def _summarize_for_memory(tool_name: str, result: dict) -> dict:
    """Compress a tool result into the small summary stored as agent memory."""
    if tool_name == "shortest_dependency_path_tool":
        chain = result.get("chain") or []
        return {
            "importer_count": 0,
            "test_count": 0,
            "top_test": "",
            "text": f"Shortest path {result.get('from')} -> {result.get('to')}: "
                    f"{result.get('hops', '?')} hops via {' -> '.join(chain)}."
                    if result.get("connected") else
                    f"No dependency path from {result.get('from')} to {result.get('to')}.",
        }
    if tool_name == "find_impact_communities_tool":
        return {
            "importer_count": 0,
            "test_count": 0,
            "top_test": "",
            "text": (f"Blast radius spans {result.get('impacted_communities', '?')} of "
                     f"{result.get('community_count', '?')} architectural communities "
                     f"(target in community {result.get('target_community', '?')})."),
        }
    ranked = result.get("ranked_tests") or []
    tests = result.get("recommended_tests") or []
    top_test = ranked[0]["path"] if ranked else (tests[0]["path"] if tests else "")
    return {
        "importer_count": int(result.get("impact_count", 0) or 0),
        "test_count": len(ranked or tests),
        "top_test": top_test,
        "text": (f"{result.get('impact_count', 0)} transitive importers, "
                 f"{len(ranked or tests)} tests; top-ranked test {top_test}." if top_test else
                 f"{result.get('impact_count', 0)} transitive importers, {len(tests)} tests."),
    }


@app.post("/api/agent/run")
async def agent_run(request: AgentRunRequest):
    if not graph_status()["configured"]:
        raise HTTPException(
            status_code=503,
            detail="FalkorDB is not connected. Configure the instance in wry_hackathon/.env first.",
        )
    return await run_agent_loop(request.prompt, request.target_path, request.other_path)


async def run_agent_loop(prompt: str, target_path: str, other_path: str | None = None, max_steps: int = 5):
    """Bounded Thought -> Action -> Observation agent loop over the MCP tools.

    The policy is deterministic and narrated honestly: each thought describes
    the real rule that chose the next tool, and each observation summarizes the
    tool's actual output. The typical change investigation runs five steps:
    trace impact -> rank tests -> blast-radius communities -> recall memory ->
    propose plan. The write tool (create_test_plan_tool) is never executed by
    the loop — it is proposed and waits for human approval.
    """
    steps: list[dict] = []
    observations: dict[str, str] = {}
    final_result = None
    proposed_action = None
    trace_result = None
    rank_result = None
    communities_result = None

    async def take_step(thought: str, tool_name: str, arguments: dict):
        nonlocal final_result, trace_result, rank_result, communities_result
        tool_result = await call_mcp_tool(tool_name, arguments)
        final_result = tool_result
        if tool_name == "trace_change_impact_tool":
            trace_result = tool_result
        if tool_name == "rank_tests_pagerank_tool":
            rank_result = tool_result
        if tool_name == "find_impact_communities_tool":
            communities_result = tool_result
        observation = _summarize_observation(tool_name, tool_result)
        observations[tool_name] = observation
        steps.append({
            "step": len(steps) + 1,
            "thought": thought,
            "action": tool_name,
            "observation": observation,
        })
        return tool_result

    words = set(prompt.lower().replace("?", " ").replace("/", " ").split())
    wants_rank = bool(words.intersection({"rank", "ranking", "ranked", "riskiest", "prioritize", "prioritise", "priority"}))
    wants_plan = bool(words.intersection({"save", "plan", "propose", "proposal"}))
    first_tool = None

    # A shortest-path question names two modules. If only one is selected,
    # ask for the destination instead of silently running impact tracing —
    # a path query needs both endpoints.
    if not other_path and words.intersection({"shortest", "between"}):
        thought = ("The request asks for a shortest dependency path, but only one module "
                   "was selected. A path needs two endpoints, so I'll ask for the destination "
                   "instead of guessing one.")
        steps.append({
            "step": 1,
            "thought": thought,
            "action": "awaiting_input",
            "observation": "No graph query was run. Waiting for the user to pick a destination module (PATH TO).",
        })
        return {
            "steps": steps,
            "final_answer": (f"To find the shortest dependency path from {target_path}, select a destination "
                             "module in PATH TO — or name the second module. I need both endpoints before "
                             "I can query FalkorDB."),
            "proposed_action": None,
            "result": None,
            "needs_input": "destination_module",
        }

    if other_path:
        await take_step(
            f"{target_path} and {other_path} were both selected. I'll find the shortest "
            "dependency path between them in FalkorDB first.",
            "shortest_dependency_path_tool",
            {"from_path": target_path, "to_path": other_path},
        )
        first_tool = "shortest_dependency_path_tool"
    else:
        first_tool, _, _, _ = await choose_analysis_tool(prompt)
        if first_tool == "list_code_targets":
            await take_step(
                "The request asks for the available modules, so I'll list what FalkorDB has indexed.",
                "list_code_targets", {},
            )
        elif first_tool == "recall_investigations_tool":
            await take_step(
                "The request asks about past work. I'll recall this module's investigation history from FalkorDB.",
                "recall_investigations_tool", {"target_path": target_path},
            )
        elif first_tool == "find_bridge_modules_tool":
            await take_step(
                "The request asks for choke points. I'll run betweenness centrality over the import graph in FalkorDB.",
                "find_bridge_modules_tool", {},
            )
        elif first_tool == "find_impact_communities_tool":
            await take_step(
                "The request asks about blast-radius communities. I'll run CDLP community detection over the import graph in FalkorDB.",
                "find_impact_communities_tool", {"target_path": target_path},
            )
        else:
            await take_step(
                "The request concerns a change. First I'll trace transitive importers and covering tests in FalkorDB.",
                "trace_change_impact_tool", {"target_path": target_path},
            )
            if wants_rank and len(steps) < max_steps:
                await take_step(
                    "The trace is in. The request asks to prioritize, so next I'll rank "
                    "the recommended tests with PageRank over the import graph.",
                    "rank_tests_pagerank_tool", {"target_path": target_path},
                )
    # Group the blast radius into architectural communities when the question
    # is about a change (trace or rank ran). Not for listing, recall, bridge
    # overview, or path questions, which are not change investigations.
    if len(steps) < max_steps and first_tool in {"trace_change_impact_tool", "rank_tests_pagerank_tool"}:
        await take_step(
            "The trace is in. Next I'll group the change's blast radius into architectural "
            "communities with CDLP community detection in FalkorDB.",
            "find_impact_communities_tool", {"target_path": target_path},
        )
    # The agent always consults its memory before answering a change question.
    if len(steps) < max_steps and first_tool not in {"recall_investigations_tool", "list_code_targets"}:
        await take_step(
            "Before answering, I'll check whether this module has been investigated before — "
            "the agent remembers past work.",
            "recall_investigations_tool", {"target_path": target_path},
        )
    if wants_plan and len(steps) < max_steps:
        tests_preview: list[str] = []
        ranked = (rank_result.get("ranked_tests") or []) if isinstance(rank_result, dict) else []
        traced = (trace_result.get("recommended_tests") or []) if isinstance(trace_result, dict) else []
        tests_preview = [t["path"] for t in ranked] or [t["path"] for t in traced]
        proposed_action = {
            "tool": "create_test_plan_tool",
            "arguments": {"target_path": target_path, "rationale": prompt[:500]},
            "tests": tests_preview,
            "proposal_token": _issue_plan_approval(target_path, tests_preview),
            "message": ("The request asks to save a plan. I'm proposing the ranked test plan "
                        "below — nothing is written until you approve it."),
        }
        steps.append({
            "step": len(steps) + 1,
            "thought": ("The request asks to save a plan. I will not write to the graph on my "
                        "own — I'll propose the plan and wait for approval."),
            "action": "proposed:create_test_plan_tool",
            "observation": f"Proposed a test plan with {len(tests_preview)} tests, ordered by PageRank importance.",
        })

    final_answer = _compose_final_answer(target_path, steps, observations, proposed_action)
    if trace_result is not None:
        try:
            record_investigation(
                target_path, "agent_loop",
                _summarize_for_memory("trace_change_impact_tool", trace_result))
        except Exception:
            pass  # memory is best-effort; never fail a run over it
    display_result = rank_result or trace_result or communities_result or final_result
    if (communities_result is not None and isinstance(display_result, dict)
            and display_result.get("tool") != "find_impact_communities"):
        # The blast-radius analysis belongs next to the trace/ranking in the UI,
        # even though the loop's primary display result is the ranked trace.
        # Its Cypher and timings merge too, so the GRAPH QUERY panel stays honest.
        # (When communities was the loop's own first tool, the payload is already
        # the display result — no merge needed.)
        display_result = {
            **display_result,
            "communities": communities_result,
            "cypher": {**(display_result.get("cypher") or {}),
                       **(communities_result.get("cypher") or {})},
            "timings": {**(display_result.get("timings") or {}),
                        **(communities_result.get("timings") or {})},
        }
    return {
        "steps": steps,
        "final_answer": final_answer,
        "proposed_action": proposed_action,
        "result": display_result,
    }


def _summarize_observation(tool_name: str, result) -> str:
    if isinstance(result, list):
        return f"FalkorDB lists {len(result)} indexed modules."
    if not isinstance(result, dict):
        return "Tool completed."
    if tool_name == "trace_change_impact_tool":
        return (f"FalkorDB found {result.get('impact_count', 0)} transitive importers and "
                f"{len(result.get('recommended_tests', []))} covering tests "
                f"(repository surface {result.get('change_surface_score', 0)}%).")
    if tool_name == "rank_tests_pagerank_tool":
        ranked = result.get("ranked_tests", [])
        if not ranked:
            return "PageRank returned no tests to rank."
        top = ranked[0]
        return (f"PageRank ranked {len(ranked)} tests; riskiest first is {top['path']} "
                f"(score {top['pagerank_score']}).")
    if tool_name == "shortest_dependency_path_tool":
        if result.get("connected"):
            chain = " -> ".join(p.split("/")[-1] for p in result["chain"])
            return f"Shortest path is {result['hops']} hops: {chain}."
        return f"No dependency path from {result.get('from')} to {result.get('to')} within 10 hops."
    if tool_name == "find_bridge_modules_tool":
        bridges = result.get("bridges", [])
        top = bridges[0]["path"] if bridges else "none"
        return f"Betweenness over the import graph: top choke point is {top} ({len(bridges)} modules scored)."
    if tool_name == "find_impact_communities_tool":
        return (f"CDLP found {result.get('community_count', 0)} architectural communities; "
                f"the change touches {result.get('impacted_communities', 0)} of them "
                f"(target in community {result.get('target_community', '?')}).")
    if tool_name == "recall_investigations_tool":
        count = result.get("investigation_count", 0)
        if not count:
            return "No past investigations of this module — this run will be remembered."
        latest = result["investigations"][0]
        return (f"Remembered {count} past investigation(s); the most recent found "
                f"{latest.get('importer_count', 0)} importers and {latest.get('test_count', 0)} tests.")
    return "Tool completed."


def _compose_final_answer(target_path: str, steps: list[dict], observations: dict[str, str],
                          proposed_action: dict | None) -> str:
    parts = [f"Investigated {target_path} in {len(steps)} step(s)."]
    for key in ("list_code_targets", "trace_change_impact_tool", "rank_tests_pagerank_tool",
                "shortest_dependency_path_tool", "find_bridge_modules_tool",
                "find_impact_communities_tool", "recall_investigations_tool"):
        if key in observations:
            parts.append(observations[key])
    if proposed_action:
        parts.append(proposed_action["message"])
    return " ".join(parts)


@app.post("/api/agent/test-plan")
async def test_plan(request: PlanRequest):
    # This POST is the user's approval gesture: it flips the proposal to
    # "approved" and then executes the write. The MCP tool itself only
    # accepts approved tokens, so a proposal token grabbed from an
    # investigate/run response cannot be used to bypass approval by calling
    # the MCP tool directly.
    try:
        _approve_plan_proposal(request.proposal_token, request.target_path)
    except PlanApprovalError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return await call_mcp_tool(
        "create_test_plan_tool",
        {"target_path": request.target_path, "rationale": request.rationale,
         "proposal_token": request.proposal_token},
    )


@app.get("/api/agent/evaluate")
async def evaluate():
    if not graph_status()["configured"]:
        raise HTTPException(
            status_code=503,
            detail="FalkorDB is not connected. Configure the instance in wry_hackathon/.env first.",
        )
    try:
        return await asyncio.to_thread(run_evaluation)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"FalkorDB evaluation unavailable: {exc}") from exc
