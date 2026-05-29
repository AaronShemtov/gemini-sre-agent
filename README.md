# AI SRE Agent for OKE

A LangGraph-based Kubernetes incident-investigation agent for Oracle
Kubernetes Engine. You ask it — in Telegram — *"why is cv-frontend
unhealthy?"* and it gathers pods, events, logs, deployment/service/endpoint
state from the live cluster, then returns a root-cause analysis: probable
cause, timeline, blast radius, evidence, and a suggested fix. When a safe
remediation applies (rolling restart or fixing a service selector) it asks
for your approval before touching anything.

## Architecture 

```
Telegram (DM, whitelisted users)
        │  long polling
        ▼
   python-telegram-bot  ──►  approval gate (inline buttons)
        │
        ▼
   LangGraph agent
   plan → collect → replan → collect → analyse → (propose action)
        │                                              │ approved
        │ read-only K8s API                            ▼
        ▼                                     execute → verify
   Kubernetes API (in-cluster ServiceAccount, RBAC-scoped)
        │
        ▼
   Gemini 2.0 Flash  (planning + RCA reasoning)
```

- **Gemini** is the brain: it plans which signals to gather, then reasons
  over them to produce the RCA. Two planning rounds (broad discovery, then
  targeted drill-down on the specific failing pod) keep token usage low.
- **Kubernetes API** is the hands: a tightly RBAC-scoped ServiceAccount.
  Broad **read**, almost no **write**.
- **LangGraph** is the nervous system: a deterministic graph so we control
  exactly which cluster calls happen — the LLM proposes, the graph
  disposes.
- **Telegram** is the interface, with a hard user whitelist and a human
  approval gate on every mutating action.

## Safety model

The agent's ClusterRole grants:

- **read** (get/list/watch) on pods, pods/log, events, deployments,
  replicasets, statefulsets, daemonsets, services, endpoints, ingresses,
  httproutes, configmaps, nodes, namespaces;
- **patch** on **deployments** (rolling restart) and **services** (selector
  fix) — and nothing else.

Explicitly **not** granted: reading Secrets, `pods/exec`, `delete` on
anything, or any wildcard. Every mutating action is also gated behind a
Telegram Approve/Reject button, and the bot obeys only whitelisted user
IDs (`TELEGRAM_ALLOWED_USERS`). If the whitelist is empty, it refuses
everyone (fail-closed).

## Tools

Read: `get_pods`, `describe_pod`, `get_pod_logs` (incl. previous-container
logs for crashloops), `get_events`, `get_deployment`, `get_service`,
`get_endpoints`.

Safe write (approval-gated): `rollout_restart`, `patch_service_selector`.

## Prerequisites

Two secrets in OCI Vault:
- `gemini-api-key` — your Google AI Studio key
- `telegram-bot-token` — the bot token (reused from the Flux notifications
  setup; one bot both notifies the channel and answers your DMs)

Your Telegram numeric user ID (message `@userinfobot` to get it).

## Deploy

1. **Set your Telegram user ID** in `k8s/deployment.yaml`'s ConfigMap
   (`TELEGRAM_ALLOWED_USERS`). This is not a secret — it's an allow-list.

2. **Create the OCIR repo** `gemini-sre-agent` (first push from CI creates
   the image; create the repo in the OCI console if your tenancy needs it
   pre-created).

3. **Push the code repo** — GitHub Actions builds the ARM64 image, pushes
   to OCIR, and sends a Telegram build notification (same pipeline as the
   other services).

4. **Add the manifests to `personal-k8s`** under
   `infrastructure/gemini-sre-agent/` and let Flux reconcile, OR apply
   directly:

   ```powershell
   kubectl apply -f k8s/namespace.yaml
   kubectl apply -f k8s/rbac.yaml
   kubectl apply -f k8s/externalsecrets.yaml
   kubectl apply -f k8s/deployment.yaml
   ```

5. **Verify**:

   ```powershell
   kubectl get pods -n ai-sre-agent
   kubectl logs -n ai-sre-agent deploy/ai-sre-agent
   # → "AI SRE Agent starting (long polling)…"
   ```

6. **Use it** — DM your bot:

   ```
   why is cv-frontend unhealthy in namespace cv
   /investigate urlshortener-reader keeps restarting
   ```

## Notes / limitations (MVP)

- Single replica by design — Telegram long polling allows only one
  consumer per bot token (two would 409). Strategy is `Recreate`.
- Approval state is held in memory; if the pod restarts between proposing
  an action and your approval, you'll be asked to re-run. Fine for a
  single-user MVP; a durable store (or LangGraph checkpointer) is the
  next step.
- Logs come straight from the Kubernetes API (pod logs + previous
  container logs). A future iteration can add Loki as a second source for
  history beyond what's still on the node.

## Roadmap

- Deployment-failure debugger mode (correlate CI/CD + Helm + rollout).
- ChatOps commands (`/status`, `/rollout-status`, `/restart`).
- Loki + Prometheus as additional signal sources.
- Web UI at `agent.1ms.my` reusing the same graph.
