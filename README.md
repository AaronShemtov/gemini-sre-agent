# AI SRE Agent for OKE

A LangGraph-based Kubernetes incident-investigation agent for Oracle
Kubernetes Engine. You ask it — in Telegram — *"why is cv-frontend
unhealthy?"* and it gathers pods, events, logs, deployment/service/endpoint
state from the live cluster, then returns a root-cause analysis: probable
cause, timeline, blast radius, evidence, and a suggested fix. When a safe
remediation applies (rolling restart or fixing a service selector) it asks
for your approval before touching anything.

The project is provider-neutral at the product level. Gemini is the current
LLM provider and can be replaced or complemented by other providers later.

## Architecture

```
Telegram (DM, whitelisted users)
        │  long polling
        ▼
   python-telegram-bot  ──►  approval gate (inline buttons)
        │
        ▼
   LangGraph agent
   gather → analyse → (propose action)
        │                         │ approved
        │ read-only K8s API       ▼
        ▼                  execute → verify
   Kubernetes API (in-cluster ServiceAccount, RBAC-scoped)
        │
        ▼
   Gemini (current planning + RCA provider)
```

- **Gemini** is the current brain: it reasons over collected signals to
  produce the RCA. The provider-specific `GEMINI_*` settings remain explicit.
- **Kubernetes API** is the hands: a tightly RBAC-scoped ServiceAccount.
  Broad **read**, almost no **write**.
- **LangGraph** is the nervous system: a deterministic graph so we control
  exactly which cluster calls happen — the LLM proposes, the graph disposes.
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

Secrets in OCI Vault:

- `gemini-api-key` — Google AI Studio key for the current provider;
- `sre-agent-telegram-bot-token` — dedicated bot token for the agent.

GitHub Actions secrets:

- `OCIR_AUTH_TOKEN`;
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for build notifications.

Create the OCIR repository `ai-sre-agent` manually in compartment `personal`
**before the first push**. Otherwise OCIR may auto-create it in the root
compartment, outside the expected IAM policies.

## Deploy

1. Set `TELEGRAM_ALLOWED_USERS` in
   `personal-k8s/applications/ai-sre-agent/deployment.yaml`.
2. Merge code to `main`. GitHub Actions builds the `linux/arm64` image and
   pushes it to `il-jerusalem-1.ocir.io/axiwhc5fgu9i/ai-sre-agent` with
   version, short-SHA and `latest` tags.
3. Review and merge the corresponding GitOps pull request in `personal-k8s`.
   Flux performs the deployment; do not apply these resources manually.
4. Verify the `ai-sre-agent` Deployment and pod in namespace `ai-sre-agent`.

## Usage

DM the configured Telegram bot, for example:

```
why is cv-frontend unhealthy in namespace cv
/investigate urlshortener-reader keeps restarting
```

## Notes / limitations

- Single replica by design — Telegram long polling allows only one
  consumer per bot token (two would return 409). Strategy is `Recreate`.
- Approval state is held in memory; if the pod restarts between proposing
  an action and approval, the investigation must be run again.
- Logs come directly from the Kubernetes API. Loki can be added later for
  history beyond logs retained on the node.
- Gemini is currently the only implemented LLM provider, but it is not part
  of the project identity.

## Roadmap

- Provider abstraction for additional hosted and local LLMs.
- Deployment-failure debugger mode (correlate CI/CD + rollout).
- ChatOps commands (`/status`, `/rollout-status`, `/restart`).
- Loki + Prometheus as additional signal sources.
- Web UI at `agent.1ms.my` reusing the same graph.
