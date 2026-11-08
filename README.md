# Daalu — open-source AI operations for infrastructure you own

Daalu is a self-hostable operations platform for teams that run real
infrastructure: Kubernetes clusters, GPU nodes serving LLMs, data-center
switches, and the observability stack around them. It puts those systems in
one place and pairs them with an AI agent that investigates alerts with
read-only tools and proposes fixes. **By default nothing changes until a
human approves it**; an autonomy policy written as code decides which proven,
low-risk fixes may run on their own, under a kill switch and circuit breaker.

The model can be one you host yourself (vLLM on your own NVIDIA GPU) or a
hosted API. The platform runs the same way with either.

> Copyright © 2026 Kezie Iwueke. Licensed under [Apache-2.0](LICENSE).

## AI-Ops Maturity Model

Daalu implements the **AI-Ops Maturity Model (AOMM)**: a standard for how
infrastructure teams integrate AI, the way DevOps did for delivery. Like DORA,
it starts with metrics. Daalu computes them from what actually happened in the
last 30 days and places your team on one of six levels from them
(**AI Ops → Maturity** in the UI).

**Five TRACE metrics**, the AOMM counterpart of the DORA four keys. **TRACE** is their initials: **T**ime to triage, time to **R**esolve, **A**greement rate, **C**hange-failure rate, **E**scalation rate.

| Group | Metric | Question it answers | DORA counterpart |
|---|---|---|---|
| Speed | Time to triage | Alert firing → AI names a root cause, how long? | Lead time for changes |
| Speed | Time to resolve | Alert firing → incident resolved, how long? | Time to restore service |
| Quality | Agreement rate | How often does the team agree with the AI's root cause? | — |
| Quality | Change-failure rate | Of the fixes the AI ran, how many didn't fix the problem? | Change-failure rate |
| Autonomy | Escalation rate | Of resolved incidents, how many still needed a human? | — |

Supporting metrics explain them: alert volume and AI triage rate (coverage),
verdict coverage (how much of the AI's work humans grade), approval rate,
runbook and eval counts, and whether the hash-chained audit log verifies.

**Levels are thresholds on those metrics:**

| Level | Name | In one line | Reached when (plus every lower level) |
|---|---|---|---|
| 0 | Ad hoc | Individuals paste logs into a chatbot; nothing is shared or recorded. | Starting point |
| 1 | Assisted | AI reads alerts, logs and metrics and explains them. Read-only. | ≥ 5 alerts, triage rate ≥ 50 % |
| 2 | Proposing | AI names the root cause and proposes a fix; every change is human-approved and verified. | A human-approved fix ran; approval rate measured |
| 3 | Trusted | A policy lets low-risk, proven actions run automatically. | Change-failure rate ≤ 15 %; an automatic fix ran under a saved `auto` rule |
| 4 | Learning | Every incident improves the next: verdicts, runbooks, evals, metrics. | Verdict coverage ≥ 30 %; a runbook published; an eval run |
| 5 | Autonomous | AI resolves most routine incidents; humans own the policy and the exceptions. | Escalation rate ≤ 50 %, agreement rate ≥ 80 %, audit chain verifies, guardrails saved |

The two speed metrics gate no level, on purpose: they are what climbing the
levels should improve. The practices that move the metrics are expressed as
code (autonomy policy, runbooks, eval scenarios, the audit log).

With no saved autonomy policy, every write the AI proposes needs one human
approval, exactly as before AOMM.

- Team-facing summary and a 90-day plan: [docs/maturity-model.md](docs/maturity-model.md)
- The standard and JSON Schemas: [spec/](spec/README.md)
- Engineering manual, Part XV: [docs/book-engineer/15-ai-ops-maturity/](docs/book-engineer/15-ai-ops-maturity/README.md)
- Operator's Handbook, Part IX: [docs/book-customer/09-ai-operations-maturity/](docs/book-customer/09-ai-operations-maturity/README.md)
- Connect Claude Code or Cursor over MCP: [docs/mcp.md](docs/mcp.md)
- Benchmark your AI SRE: [evals/](evals/README.md)

---

## What it does

| Area | What you get |
|---|---|
| **AI SRE** | Alerts from Prometheus/Alertmanager are triaged by an agent that reads pod logs, events and metrics, names a root cause, and opens a per-alert chat. Write actions (restart, scale, patch, rollback) carry a reason, a dry-run preview and blast radius, and go through the **autonomy policy**: denied, queued for one or more approvers (the default), or run automatically and verified. |
| **AI-Ops maturity** | Investigation records with human verdicts, runbooks the agent reads and drafts, a `recent_changes` tool, a hash-chained audit log, AI-ops metrics and an automatic maturity level, an eval harness for your AI SRE, and an MCP server so Claude Code or Cursor work through the same policy. |
| **Cluster federation** | Manage clusters behind NAT with **zero inbound ports**: a `daalu-edge` chart dials a WireGuard hub, gets a mesh IP (default `10.200.0.0/16`), and the hub reaches its API server, Prometheus and Loki through the tunnel. |
| **GPU & LLM serving** | Zero-touch GPU node onboarding (NVIDIA GPU Operator → vLLM → registered model), DCGM metrics in a native AI Factory UI, GPU sharing (time-slicing/MPS), a **local-first LLM router** with hosted fallbacks and per-tenant cost tracking, and AIPerf load tests. |
| **Observability** | Onboard an existing Prometheus/Loki/OpenSearch, or install a stack into a cluster in one click. Grafana dashboards render natively, and the agent can generate new ones from plain English. |
| **Network source of truth** | Nautobot as the source of truth (hosted per tenant or bring your own), bulk import of cabling/addressing/intent plans, NVIDIA Config Manager integration for config generation, a live LLDP topology map, and a click-to-SSH device terminal. |
| **Coding agent** | A terminal coding agent (`daalu` CLI), a VS Code extension and browser workspaces, all backed by a coding model on your GPU through an OpenAI-compatible gateway. |
| **Platform** | Multi-tenant with RBAC, Keycloak/OIDC SSO or local accounts, personal access tokens, workflows, morning briefings, Slack/email/webhook notifications, and AWS/GCP/Azure read-only integrations. |

---

## Quick start: Docker Compose (about 10 minutes)

You need Docker with the Compose plugin, on Linux.

```bash
git clone https://github.com/kiwueke1/daalu.git
cd daalu
cp .env.example .env
# edit .env: ADMIN_EMAIL, ADMIN_PASSWORD, DAALU_HOST_IP (`hostname -I`),
# and a model: LLM_LOCAL_BASE_URL (your own vLLM/Ollama), LLM_API_KEY or ANTHROPIC_API_KEY
docker compose up -d --build
```

Then open **http://localhost:3000** and sign in with `ADMIN_EMAIL` /
`ADMIN_PASSWORD`. The API is on http://localhost:8000.

**Give it a cluster to manage.** [`demo/`](demo/README.md) builds a separate,
monitored kind cluster with apps you can break, and connects it to Daalu over
the WireGuard tunnel:

```bash
export DAALU_TOKEN=dpat_...     # Daalu → Settings → API tokens
./demo/up.sh                    # or SKIP_ONBOARD=1 ./demo/up.sh, then onboard it from the UI
./demo/break.sh                 # then watch Daalu → Alerts
```

**Prefer Kubernetes?** `make bootstrap-dev` installs Daalu with the Helm chart on
a local kind cluster (UI on http://localhost:8080), the same topology as a
production install. Both methods, step by step:
[docs/getting-started.md](docs/getting-started.md).

### Running from source (for development)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

docker run -d --name daalu-postgres -e POSTGRES_DB=daalu_automation \
  -e POSTGRES_USER=daalu -e POSTGRES_PASSWORD=daalu_password \
  -p 5432:5432 postgres:16-alpine
docker run -d --name daalu-redis -p 6379:6379 redis:7-alpine

daalu migrate                 # creates the schema on a fresh DB, upgrades after
daalu seed                    # default tenant
daalu create-admin            # your login (prompts for email + password)
daalu server --reload         # API on :8000
daalu worker                  # Celery worker
daalu beat                    # scheduler
daalu agents                  # agent host

cd frontend && cp .env.local.example .env.local
npm ci && npm run dev         # UI on :3000
```

See [docs/getting-started.md](docs/getting-started.md) for the full
walkthrough.

## Install on a real cluster

The Helm chart in [`deploy/helm/daalu-automation`](deploy/helm/daalu-automation/)
is the supported install. Build the four images and push them to a registry
your cluster can pull from, then install:

```bash
for img in "daalu-automation:." "daalu-automation-frontend:frontend" \
           "daalu-wg-hub:deploy/wg-hub" "daalu-edge:deploy/edge"; do
  name=${img%%:*}; ctx=${img#*:}
  docker build -t <registry>/$name:<tag> $ctx && docker push <registry>/$name:<tag>
done

cp .env.example .env          # production values: DB, secrets, LLM, SSO
export KUBECONFIG=~/.kube/config
REGISTRY=<registry> IMAGE_TAG=<tag> HOST=daalu.example.com ./deploy/scripts/install.sh
# private registry? create a docker-registry Secret and pass PULL_SECRET=<name>
```

You also need a DNS record for `HOST` that points at your ingress, and
cert-manager with a `letsencrypt-prod` ClusterIssuer if you want TLS
(`make bootstrap-prod` creates one). To onboard clusters over WireGuard, label
one node `daalu.io/wireguard-hub=true` and open UDP 51820 to it. If you prefer GitOps, the ArgoCD app-of-apps in
[`deploy/argocd/`](deploy/argocd/) and the from-scratch bootstrap
(`make bootstrap-prod`) install the monitoring stack, GPU Operator and the app
together. Point the ArgoCD apps at your fork and your own overlay.

To connect another cluster, install the edge chart there. The hub UI
(**Managed infra → Clusters & observability → Onboard cluster**) prints the exact command:

```bash
helm install daalu-edge deploy/helm/daalu-edge -n daalu-edge --create-namespace \
  --set hub.endpoint=wg.example.com:51820 --set cluster.tunnelIp=10.200.0.2 ...
```

---

## Repository layout

```
.
├── src/daalu_automation/
│   ├── api/                     # FastAPI routers + schemas
│   ├── core/                    # events, agents, LLM router, WireGuard, SoT …
│   ├── models/                  # SQLAlchemy models
│   ├── modules/                 # per-domain logic (infra, …)
│   ├── workers/                 # Celery app + agent host
│   ├── gpu_controller/          # zero-touch GPU onboarding, AIPerf
│   ├── nautobot_controller/     # per-tenant Nautobot
│   ├── config_manager_controller/  # NVIDIA Config Manager deployer
│   ├── workspace_controller/    # browser coding workspaces
│   └── inference_gateway/       # OpenAI-compatible coding gateway
├── src/nv_config_manager_installer/  # vendored NVIDIA installer (Apache-2.0)
├── frontend/                    # Next.js UI
├── cli/                         # `daalu` terminal coding agent
├── extensions/daalu-assistant/  # VS Code extension
├── components/nautobot/         # vendored NV-CM Nautobot jobs
├── deploy/
│   ├── helm/                    # daalu-automation, daalu-edge, … charts
│   ├── k8s/                     # kustomize base (app, monitoring, GPU, Nautobot)
│   ├── argocd/                  # app-of-apps
│   ├── charts/                  # vendored NV-CM charts
│   ├── edge/, wg-hub/           # WireGuard edge + hub images
│   └── scripts/
├── dev/                         # kind bootstrap + Tiltfile
├── demo/                        # monitored kind lab with breakable apps
├── evals/                       # eval scenarios for the AI SRE (`daalu eval`)
├── runbooks/                    # runbooks as code (`daalu runbooks import`)
├── spec/                        # the AI-Ops Maturity Model standard + JSON Schemas
├── migrations/                  # Alembic
├── docs/                        # architecture, guides, handbooks
└── tests/
```

## Documentation

- [docs/architecture.md](docs/architecture.md): how the pieces fit together
- [docs/system-walkthrough.md](docs/system-walkthrough.md): low-level design with code references
- [docs/getting-started.md](docs/getting-started.md): installing with Docker Compose or Kubernetes, and adding a cluster to manage
- [docs/extending.md](docs/extending.md): adding a domain module
- [docs/cluster-tunnel-feature.md](docs/cluster-tunnel-feature.md): the WireGuard federation design
- [docs/sot-device-management/](docs/sot-device-management/): Nautobot source of truth and device onboarding
- [docs/book-customer/](docs/book-customer/README.md): the user handbook (features, integrations, troubleshooting)
- [docs/book-engineer/](docs/book-engineer/README.md): the contributor handbook (backend, frontend, federation, GPU, security, testing)
- [docs/runbooks/](docs/runbooks/README.md): runbooks linked from the shipped alert rules
- [docs/maturity-model.md](docs/maturity-model.md): the AI-Ops Maturity Model for your team, and [spec/](spec/README.md): the standard itself
- [docs/mcp.md](docs/mcp.md): connecting Claude Code, Cursor or any MCP client
- [evals/](evals/README.md): benchmarking your AI SRE

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).
To report a vulnerability, see [SECURITY.md](SECURITY.md); please don't open a
public issue for it.

Third-party code vendored under `src/nv_config_manager_installer/`,
`components/nautobot/` and `deploy/charts/` keeps its upstream license (see
`src/nv_config_manager_installer/VENDORED.md`, `components/VENDORED.md` and
`deploy/charts/README.md`).
