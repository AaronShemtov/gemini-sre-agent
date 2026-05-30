"""
The investigation graph (LangGraph) -- single-LLM-call design.

Flow:

    gather  ──►  analyse  ──►  END
   (no LLM)     (1 Gemini call)

`gather` is pure deterministic code: it resolves the namespace from the
question, then does a fixed broad sweep of READ tools (pods + events, then
describe + logs for any unhealthy pod, plus deployment/service/endpoints
for the workloads it finds). No LLM is used to decide what to look at --
that keeps us to exactly ONE Gemini call per investigation, which matters
on the free tier (RPD 20).

`analyse` is the single Gemini call: it turns the gathered signals into an
RCA and may propose ONE safe remediation. If it does, the Telegram layer
asks for human approval and, if granted, runs RESUME_GRAPH (execute ->
verify). Mutating actions never run inside this graph.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

from app.graph import llm
from app.tools.registry import REGISTRY
from app.k8s import client as k8s

logger = logging.getLogger(__name__)


class AgentState(TypedDict, total=False):
    question: str
    namespace: str
    collected: list[dict[str, Any]]   # [{tool, args, result}]
    analysis: dict[str, Any]
    proposed_action: Optional[dict]
    approved: Optional[bool]
    action_result: Optional[dict]
    verification: Optional[str]
    final: str


# ---------------------------------------------------------------------
# Deterministic signal gathering (no LLM)
# ---------------------------------------------------------------------

# Pod phases / reasons we treat as "interesting enough to dig into".
_UNHEALTHY_HINTS = {
    "CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull",
    "Pending", "CreateContainerConfigError", "OOMKilled", "Failed",
    "ContainerCreating", "Init:Error", "RunContainerError",
}


def _resolve_namespace(question: str, namespaces: list[str]) -> str:
    """Pick the namespace from the question by exact-word match against the
    real namespace list. Falls back to any namespace mentioned as a word,
    else 'default'. Deterministic -- no LLM."""
    q = question.lower()
    # Prefer the longest namespace name that appears as a token in the text
    # (so 'urlshortener' wins over a hypothetical 'url').
    candidates = sorted(namespaces, key=len, reverse=True)
    for ns in candidates:
        if re.search(rf"\b{re.escape(ns.lower())}\b", q):
            return ns
    return "default"


def _is_unhealthy(pod: dict[str, Any]) -> bool:
    if pod.get("phase") not in ("Running", "Succeeded"):
        return True
    if pod.get("reason") in _UNHEALTHY_HINTS:
        return True
    # ready like "0/1" or restarts piling up
    ready = str(pod.get("ready", ""))
    if "/" in ready:
        have, want = ready.split("/", 1)
        if have != want:
            return True
    if isinstance(pod.get("restarts"), int) and pod["restarts"] >= 3:
        return True
    return False


def _add(collected: list[dict], tool: str, args: dict, result: Any) -> None:
    collected.append({"tool": tool, "args": args, "result": result})


def _safe(fn, **kwargs):
    try:
        return fn(**kwargs)
    except Exception as e:  # noqa: BLE001 -- surface to the LLM as text
        return f"(error: {type(e).__name__}: {e})"


def node_gather(state: AgentState) -> AgentState:
    """Broad, fixed read-only sweep. Deterministic; uses no LLM."""
    namespaces = k8s.list_namespaces()
    ns = _resolve_namespace(state["question"], namespaces)
    collected: list[dict] = []

    # 1) pods + events -- the baseline picture
    pods = _safe(k8s.get_pods, namespace=ns)
    _add(collected, "get_pods", {"namespace": ns}, pods)
    _add(collected, "get_events", {"namespace": ns}, _safe(k8s.get_events, namespace=ns))

    # 2) for each unhealthy pod, pull describe + logs (incl. previous)
    unhealthy = [p for p in pods if isinstance(p, dict) and _is_unhealthy(p)] if isinstance(pods, list) else []
    # cap to avoid giant prompts / many API reads on a broad outage
    for p in unhealthy[:5]:
        name = p.get("name")
        if not name:
            continue
        _add(collected, "describe_pod", {"namespace": ns, "pod": name},
             _safe(k8s.describe_pod, namespace=ns, pod=name))
        _add(collected, "get_pod_logs", {"namespace": ns, "pod": name},
             _safe(k8s.get_pod_logs, namespace=ns, pod=name, tail=80))

    # 3) workload/service context -- derive names from pod labels/owners.
    #    We use the pod name prefix as a best-effort deployment/service name
    #    guess, de-duplicated. This is heuristic but cheap and LLM-free.
    seen: set[str] = set()
    sample_pods = unhealthy or (pods if isinstance(pods, list) else [])
    for p in sample_pods[:5]:
        name = p.get("name", "") if isinstance(p, dict) else ""
        # strip the trailing replicaset/pod hash suffixes: name-xxxx-yyyy
        base = re.sub(r"-[a-z0-9]{5,10}(-[a-z0-9]{5})?$", "", name)
        if not base or base in seen:
            continue
        seen.add(base)
        _add(collected, "get_deployment", {"namespace": ns, "name": base},
             _safe(k8s.get_deployment, namespace=ns, name=base))
        _add(collected, "get_service", {"namespace": ns, "name": base},
             _safe(k8s.get_service, namespace=ns, name=base))
        _add(collected, "get_endpoints", {"namespace": ns, "service": base},
             _safe(k8s.get_endpoints, namespace=ns, service=base))

    return {**state, "namespace": ns, "collected": collected}


def _collected_text(collected: list[dict]) -> str:
    return json.dumps(collected, indent=2, default=str)[:24000]


def node_analyse(state: AgentState) -> AgentState:
    """The single Gemini call."""
    analysis = llm.investigate(
        state["question"], state["namespace"], _collected_text(state["collected"])
    )
    proposed = analysis.get("proposed_action")
    if proposed:
        tool = REGISTRY.get(proposed.get("tool"))
        if tool is None or not tool.mutating:
            proposed = None
    return {**state, "analysis": analysis, "proposed_action": proposed}


# ---------------------------------------------------------------------
# Resume graph: execute -> verify (run after human approval)
# ---------------------------------------------------------------------

def node_execute(state: AgentState) -> AgentState:
    action = state["proposed_action"]
    tool = REGISTRY[action["tool"]]
    args = dict(action.get("args") or {})
    try:
        result = tool.fn(**args)
    except Exception as e:  # noqa: BLE001
        result = {"error": f"{type(e).__name__}: {e}"}
    return {**state, "action_result": result}


def node_verify(state: AgentState) -> AgentState:
    action = state["proposed_action"]
    args = action.get("args", {})
    ns = args.get("namespace", state.get("namespace"))
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


# ---------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------

def build_graph():
    """Investigation graph: gather -> analyse -> END. One Gemini call."""
    g = StateGraph(AgentState)
    g.add_node("gather", node_gather)
    g.add_node("analyse", node_analyse)
    g.set_entry_point("gather")
    g.add_edge("gather", "analyse")
    g.add_edge("analyse", END)
    return g.compile()


def build_resume_graph():
    """Resume after human approval: execute -> verify."""
    g = StateGraph(AgentState)
    g.add_node("execute", node_execute)
    g.add_node("verify", node_verify)
    g.set_entry_point("execute")
    g.add_edge("execute", "verify")
    g.add_edge("verify", END)
    return g.compile()


GRAPH = build_graph()
RESUME_GRAPH = build_resume_graph()
