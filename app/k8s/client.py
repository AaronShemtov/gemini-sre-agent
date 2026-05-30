"""
Kubernetes API client wrapper — the agent's "hands".

Uses the in-cluster ServiceAccount credentials (mounted automatically when
running as a pod). All read operations are plain GETs; the two write
operations (rollout restart, service selector patch) are deliberately the
ONLY mutating calls the agent can make, and they are gated behind an
approval step in the graph.

Everything returns plain dict/str so it can be fed straight into the LLM
prompt without custom serialisation.
"""

from __future__ import annotations

import datetime
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException


def _load_config() -> None:
    """Load in-cluster config when running as a pod, else fall back to
    local kubeconfig (useful for running the agent on a laptop during dev)."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


_load_config()

_core = client.CoreV1Api()
_apps = client.AppsV1Api()
_net = client.NetworkingV1Api()


def _age(ts: datetime.datetime | None) -> str:
    """Human-readable age from a creation timestamp."""
    if ts is None:
        return "unknown"
    delta = datetime.datetime.now(datetime.timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# ─────────────────────────────────────────────────────────────────────
# READ TOOLS
# ─────────────────────────────────────────────────────────────────────

def get_pods(namespace: str) -> list[dict[str, Any]]:
    """List pods in a namespace with status, restarts, readiness, node."""
    pods = _core.list_namespaced_pod(namespace).items
    out = []
    for p in pods:
        statuses = p.status.container_statuses or []
        restarts = sum(cs.restart_count for cs in statuses)
        ready = sum(1 for cs in statuses if cs.ready)
        total = len(statuses)
        # Surface the most informative waiting/terminated reason if any.
        reason = p.status.phase
        for cs in statuses:
            st = cs.state
            if st.waiting and st.waiting.reason:
                reason = st.waiting.reason
                break
            if st.terminated and st.terminated.reason:
                reason = st.terminated.reason
                break
        out.append(
            {
                "name": p.metadata.name,
                "phase": p.status.phase,
                "reason": reason,
                "ready": f"{ready}/{total}",
                "restarts": restarts,
                "node": p.spec.node_name,
                "age": _age(p.metadata.creation_timestamp),
                # Labels are essential for diagnosing service-selector
                # mismatches: the agent compares these against a Service's
                # selector to decide whether (and how) to fix it.
                "labels": p.metadata.labels or {},
            }
        )
    return out


def describe_pod(namespace: str, pod: str) -> dict[str, Any]:
    """Detailed single-pod view: containers, images, probes, resource
    requests/limits, recent state transitions and conditions."""
    p = _core.read_namespaced_pod(pod, namespace)
    containers = []
    for c in p.spec.containers:
        res = c.resources
        containers.append(
            {
                "name": c.name,
                "image": c.image,
                "requests": (res.requests if res and res.requests else {}),
                "limits": (res.limits if res and res.limits else {}),
                "liveness": _probe_summary(c.liveness_probe),
                "readiness": _probe_summary(c.readiness_probe),
                "env": [e.name for e in (c.env or [])],
            }
        )

    statuses = []
    for cs in p.status.container_statuses or []:
        st = cs.state
        cur = "running"
        detail = {}
        if st.waiting:
            cur = "waiting"
            detail = {"reason": st.waiting.reason, "message": st.waiting.message}
        elif st.terminated:
            cur = "terminated"
            detail = {
                "reason": st.terminated.reason,
                "exit_code": st.terminated.exit_code,
                "message": st.terminated.message,
            }
        statuses.append(
            {
                "name": cs.name,
                "ready": cs.ready,
                "restarts": cs.restart_count,
                "state": cur,
                "detail": detail,
            }
        )

    conditions = [
        {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
        for c in (p.status.conditions or [])
    ]

    return {
        "name": p.metadata.name,
        "namespace": namespace,
        "phase": p.status.phase,
        "node": p.spec.node_name,
        "labels": p.metadata.labels or {},
        "start_time": str(p.status.start_time) if p.status.start_time else None,
        "containers": containers,
        "container_statuses": statuses,
        "conditions": conditions,
    }


def _probe_summary(probe: Any) -> str | None:
    if probe is None:
        return None
    if probe.http_get:
        return f"http GET {probe.http_get.path}:{probe.http_get.port}"
    if probe.tcp_socket:
        return f"tcp {probe.tcp_socket.port}"
    if probe._exec:
        return f"exec {' '.join(probe._exec.command or [])}"
    return "set"


def get_pod_logs(namespace: str, pod: str, tail: int = 100, container: str | None = None) -> str:
    """Tail logs from a pod. If the pod has crashed, also try previous
    container logs — that's usually where the actual error is."""
    try:
        current = _core.read_namespaced_pod_log(
            name=pod, namespace=namespace, tail_lines=tail, container=container,
            timestamps=True,
        )
    except ApiException as e:
        current = f"(could not read current logs: {e.reason})"

    # For crashlooping pods, previous logs hold the real cause.
    previous = ""
    try:
        previous = _core.read_namespaced_pod_log(
            name=pod, namespace=namespace, tail_lines=tail, container=container,
            previous=True, timestamps=True,
        )
    except ApiException:
        pass  # no previous instance — normal for healthy pods

    parts = [f"=== current logs ({pod}) ===", current or "(empty)"]
    if previous.strip():
        parts += [f"\n=== PREVIOUS container logs ({pod}) — likely crash cause ===", previous]
    return "\n".join(parts)


def get_events(namespace: str, limit: int = 40) -> list[dict[str, Any]]:
    """Recent events in a namespace, newest first. Events are the single
    most useful signal for 'why won't this pod start'."""
    evs = _core.list_namespaced_event(namespace).items
    # Sort by last timestamp (fall back to event time), newest first.
    def _key(e):
        return e.last_timestamp or e.event_time or e.metadata.creation_timestamp
    evs = sorted([e for e in evs if _key(e) is not None], key=_key, reverse=True)
    out = []
    for e in evs[:limit]:
        out.append(
            {
                "type": e.type,
                "reason": e.reason,
                "object": f"{e.involved_object.kind}/{e.involved_object.name}",
                "message": e.message,
                "count": e.count,
                "age": _age(_key(e)),
            }
        )
    return out


def get_deployment(namespace: str, name: str) -> dict[str, Any]:
    """Deployment status: replicas, strategy, conditions, image(s),
    and the current generation vs observed (detects stuck rollouts)."""
    d = _apps.read_namespaced_deployment(name, namespace)
    images = [c.image for c in d.spec.template.spec.containers]
    conditions = [
        {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
        for c in (d.status.conditions or [])
    ]
    return {
        "name": name,
        "namespace": namespace,
        "replicas_desired": d.spec.replicas,
        "replicas_ready": d.status.ready_replicas or 0,
        "replicas_available": d.status.available_replicas or 0,
        "replicas_updated": d.status.updated_replicas or 0,
        "images": images,
        "strategy": d.spec.strategy.type if d.spec.strategy else None,
        "generation": d.metadata.generation,
        "observed_generation": d.status.observed_generation,
        "conditions": conditions,
    }


def get_service(namespace: str, name: str) -> dict[str, Any]:
    """Service spec: type, clusterIP, ports, and — crucially — the
    selector, which is the usual culprit for 'service has no endpoints'."""
    s = _core.read_namespaced_service(name, namespace)
    ports = [
        {"port": p.port, "target_port": str(p.target_port), "protocol": p.protocol}
        for p in (s.spec.ports or [])
    ]
    return {
        "name": name,
        "namespace": namespace,
        "type": s.spec.type,
        "cluster_ip": s.spec.cluster_ip,
        "selector": s.spec.selector or {},
        "ports": ports,
    }


def get_endpoints(namespace: str, service: str) -> dict[str, Any]:
    """Endpoints behind a service. Empty addresses = nothing is serving
    traffic, which combined with the service selector usually pinpoints
    a label mismatch."""
    try:
        ep = _core.read_namespaced_endpoints(service, namespace)
    except ApiException as e:
        return {"service": service, "error": e.reason}
    ready, not_ready = [], []
    for subset in ep.subsets or []:
        for a in subset.addresses or []:
            ready.append(a.ip + (f" ({a.target_ref.name})" if a.target_ref else ""))
        for a in subset.not_ready_addresses or []:
            not_ready.append(a.ip + (f" ({a.target_ref.name})" if a.target_ref else ""))
    return {
        "service": service,
        "namespace": namespace,
        "ready_addresses": ready,
        "not_ready_addresses": not_ready,
        "total_ready": len(ready),
    }


def list_namespaces() -> list[str]:
    """All namespace names — used by the intent router to resolve fuzzy
    references like 'the payments namespace'."""
    return [ns.metadata.name for ns in _core.list_namespace().items]


# ─────────────────────────────────────────────────────────────────────
# SAFE WRITE TOOLS  (gated behind approval in the graph)
# ─────────────────────────────────────────────────────────────────────

def rollout_restart(namespace: str, deployment: str) -> dict[str, Any]:
    """Trigger a rolling restart by patching the pod template annotation,
    exactly like `kubectl rollout restart`. Safe: it does not change the
    spec, just rolls pods. Reversible by nature (just restarts again)."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": now,
                        "ai-sre-agent/restartedAt": now,
                    }
                }
            }
        }
    }
    _apps.patch_namespaced_deployment(deployment, namespace, patch)
    return {"action": "rollout_restart", "deployment": deployment, "namespace": namespace, "at": now}


def patch_service_selector(namespace: str, service: str, selector: dict[str, str]) -> dict[str, Any]:
    """Replace a service's selector. The classic fix for 'service points
    at no pods because the selector has a typo'. Returns the before/after
    so the change is auditable."""
    before = _core.read_namespaced_service(service, namespace).spec.selector or {}
    patch = {"spec": {"selector": selector}}
    _core.patch_namespaced_service(service, namespace, patch)
    return {
        "action": "patch_service_selector",
        "service": service,
        "namespace": namespace,
        "selector_before": before,
        "selector_after": selector,
    }
