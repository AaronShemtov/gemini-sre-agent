"""
The investigation graph (LangGraph).

Flow:

    plan ──► collect ──► replan ──► collect_more ──► analyse ──► decide
                                                                   │
                                              ┌────────────────────┤
                                              ▼                    ▼
                                       (no action)          (mutating action)
                                          done                 await_approval
                                                                   │ approved
                                                                   ▼
                                                                execute
                                                                   │
                                                                   ▼
                                                                 verify ──► done

For a pure read-only investigation the graph ends right after `analyse`
(decide → done). It only pauses at `await_approval` when the LLM proposed
a mutating remediation. The Telegram layer handles the actual human
yes/no via inline buttons and resumes the graph.

State is a plain dict (TypedDict). We keep `collected` as a growing list of
(tool, args, result) so both the LLM and the final report can reference it.
"""

from __future__ import annotations

import json
import time
import logging
from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

from app.graph import llm
from app.tools.registry import REGISTRY

logger = logging.getLogger(__name__)

class AgentState(TypedDict, total=False):
    question: str
    namespace: str
    collected: list[dict[str, Any]]   # [{tool, args, result}]
    analysis: dict[str, Any]          # output of llm.analyse
    proposed_action: Optional[dict]   # {tool, args, why}
    approved: Optional[bool]          # set by the Telegram approval gate
    action_result: Optional[dict]
    verification: Optional[str]
    final: str                        # rendered text answer


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _run_calls(calls: list[dict], namespace: str, collected: list[dict]) -> None:
    """Execute a list of {tool, args} read-only calls, appending results.
    Mutating tools are refused here — they only run in the execute node."""
    for call in calls:
        name = call.get("tool")
        args = dict(call.get("args") or {})
        tool = REGISTRY.get(name)
        if tool is None:
            collected.append({"tool": name, "args": args, "result": f"(unknown tool {name})"})
            continue
        if tool.mutating:
            collected.append({"tool": name, "args": args, "result": "(refused: mutating tool not allowed during investigation)"})
            continue
        # Inject namespace if the tool needs it and it wasn't supplied.
        if "namespace" not in args and namespace:
            args["namespace"] = namespace
        try:
            result = tool.fn(**args)
        except TypeError as e:
            result = f"(bad arguments: {e})"
        except Exception as e:  # noqa: BLE001 — surface any cluster error to the LLM
            result = f"(error: {type(e).__name__}: {e})"
        collected.append({"tool": name, "args": args, "result": result})


def _collected_text(collected: list[dict]) -> str:
    """Render collected signals as compact JSON for LLM prompts."""
    return json.dumps(collected, indent=2, default=str)[:24000]  # keep prompt bounded


# ─────────────────────────────────────────────────────────────────────
# Nodes
# ─────────────────────────────────────────────────────────────────────

def node_plan(state: AgentState) -> AgentState:
    from app.k8s import client as k8s
    namespaces = k8s.list_namespaces()
    
    # We do not sleep on the first node to provide immediate initial feedback
    plan = llm.plan_investigation(state["question"], namespaces)
    ns = plan.get("namespace") or (namespaces[0] if namespaces else "default")
    collected: list[dict] = []
    _run_calls(plan.get("calls", []), ns, collected)
    return {**state, "namespace": ns, "collected": collected}


def node_replan(state: AgentState) -> AgentState:
    collected = state["collected"]
    
    # Artificial delay to ensure we stay under the 5 RPM Free Tier limit
    logger.info("Pacing API calls: sleeping 12s before replanning...")
    time.sleep(12)
    
    decision = llm.replan(_collected_text(collected))
    if not decision.get("enough", False):
        _run_calls(decision.get("calls", []), state["namespace"], collected)
    return {**state, "collected": collected}


def node_analyse(state: AgentState) -> AgentState:
    # Artificial delay to ensure we stay under the 5 RPM Free Tier limit
    logger.info("Pacing API calls: sleeping 12s before final analysis...")
    time.sleep(12)
    
    analysis = llm.analyse(state["question"], _collected_text(state["collected"]))
    proposed = analysis.get("proposed_action")
    # Validate the proposed action references a real mutating tool.
    if proposed:
        tool = REGISTRY.get(proposed.get("tool"))
        if tool is None or not tool.mutating:
            proposed = None
    return {**state, "analysis": analysis, "proposed_action": proposed}


def node_execute(state: AgentState) -> AgentState:
    """Run the approved mutating action."""
    action = state["proposed_action"]
    tool = REGISTRY[action["tool"]]
    args = dict(action.get("args") or {})
    try:
        result = tool.fn(**args)
    except Exception as e:  # noqa: BLE001
        result = {"error": f"{type(e).__name__}: {e}"}
    return {**state, "action_result": result}


def node_verify(state: AgentState) -> AgentState:
    """Light verification after a mutating action: re-read the affected
    object so the final report can confirm the new state."""
    action = state["proposed_action"]
    args = action.get("args", {})
    ns = args.get("namespace", state.get("namespace"))
    from app.k8s import client as k8s
    note = ""
    try:
        if action["tool"] == "rollout_restart":
            dep = k8s.get_deployment(ns, args["deployment"])
            note = (f"deployment {args['deployment']}: "
                    f"{dep['replicas_ready']}/{dep['replicas_desired']} ready, "
                    f"updated={dep['replicas_updated']}")
        elif action["tool"] == "patch_service_selector":
            ep = k8s.get_endpoints(ns, args["service"])
            note = f"service {args['service']}: {ep['total_ready']} ready endpoints now"
    except Exception as e:  # noqa: BLE001
        note = f"(verification read failed: {e})"
    return {**state, "verification": note}


# ─────────────────────────────────────────────────────────────────────
# Graph assembly
# ─────────────────────────────────────────────────────────────────────

def build_graph():
    """Investigation graph: plan → replan → analyse → END.

    Read-only by construction. If analyse proposes a mutating action, the
    state carries `proposed_action`; the Telegram layer then asks for
    approval and, if granted, runs RESUME_GRAPH (execute → verify).

    We deliberately keep execute/verify OUT of this graph: there is no
    in-graph human-input pause in the MVP (that needs LangGraph
    checkpointing), so modelling the approval as 'end here, resume later
    in a second graph' keeps things simple and avoids unreachable nodes.
    """
    g = StateGraph(AgentState)

    g.add_node("plan", node_plan)
    g.add_node("replan", node_replan)
    g.add_node("analyse", node_analyse)

    g.set_entry_point("plan")
    g.add_edge("plan", "replan")
    g.add_edge("replan", "analyse")
    g.add_edge("analyse", END)

    return g.compile()


def build_resume_graph():
    """A tiny graph used to resume after human approval: execute → verify."""
    g = StateGraph(AgentState)
    g.add_node("execute", node_execute)
    g.add_node("verify", node_verify)
    g.set_entry_point("execute")
    g.add_edge("execute", "verify")
    g.add_edge("verify", END)
    return g.compile()


# Singletons compiled once at import.
GRAPH = build_graph()
RESUME_GRAPH = build_resume_graph()