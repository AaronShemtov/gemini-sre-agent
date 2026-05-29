"""
Tool registry. Each tool has a name, a callable, a human description, and
a `mutating` flag. The graph uses `mutating` to decide whether an action
needs to pass through the approval gate.

The LLM never calls these directly — the graph executes them and feeds the
results back. This keeps the agent deterministic about *which* cluster
calls happen (the LLM proposes, the graph disposes).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.k8s import client as k8s


@dataclass
class Tool:
    name: str
    fn: Callable[..., Any]
    description: str
    mutating: bool = False


REGISTRY: dict[str, Tool] = {
    "get_pods": Tool(
        "get_pods", k8s.get_pods,
        "List pods in a namespace with phase, readiness, restart count, node.",
    ),
    "describe_pod": Tool(
        "describe_pod", k8s.describe_pod,
        "Detailed pod view: containers, images, probes, resources, state transitions.",
    ),
    "get_pod_logs": Tool(
        "get_pod_logs", k8s.get_pod_logs,
        "Tail pod logs (and previous-container logs if it crashed).",
    ),
    "get_events": Tool(
        "get_events", k8s.get_events,
        "Recent namespace events, newest first — best signal for scheduling/start failures.",
    ),
    "get_deployment": Tool(
        "get_deployment", k8s.get_deployment,
        "Deployment status: replicas, images, rollout conditions, stuck-rollout detection.",
    ),
    "get_service": Tool(
        "get_service", k8s.get_service,
        "Service spec including the selector and ports.",
    ),
    "get_endpoints": Tool(
        "get_endpoints", k8s.get_endpoints,
        "Endpoints behind a service — empty means nothing serves traffic.",
    ),
    # ── mutating ──
    "rollout_restart": Tool(
        "rollout_restart", k8s.rollout_restart,
        "Roll all pods of a deployment (like kubectl rollout restart).",
        mutating=True,
    ),
    "patch_service_selector": Tool(
        "patch_service_selector", k8s.patch_service_selector,
        "Replace a service selector to fix a label mismatch.",
        mutating=True,
    ),
}


def tool_catalog() -> str:
    """Render the tool list for the LLM system prompt."""
    lines = []
    for t in REGISTRY.values():
        tag = " [MUTATING — needs approval]" if t.mutating else ""
        lines.append(f"- {t.name}{tag}: {t.description}")
    return "\n".join(lines)
