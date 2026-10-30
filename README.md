# Daalu — open-source AI operations for infrastructure you own

Daalu is a self-hostable operations platform for teams that run real
infrastructure: Kubernetes clusters, GPU nodes serving LLMs, data-center
switches, and the observability stack around them. It puts those systems in
one place and pairs them with an AI agent that investigates alerts with
read-only tools and proposes fixes. **Nothing changes until a human
approves it.**

The model can be one you host yourself (vLLM on your own NVIDIA GPU) or a
hosted API. The platform runs the same way with either.

> Copyright © 2026 Kezie Iwueke. Licensed under [Apache-2.0](LICENSE).

---

## What it does

| Area | What you get |
|---|---|
| **AI SRE** | Alerts from Prometheus/Alertmanager are triaged by an agent that reads pod logs, events and metrics, names a root cause, and opens a per-alert chat. Write actions (restart, scale, patch, rollback) land as **pending** proposals that carry a reason and wait for approval. |
| **Cluster federation** | Manage clusters behind NAT with **zero inbound ports**: a `daalu-edge` chart dials a WireGuard hub, gets a mesh IP (default `10.200.0.0/16`), and the hub reaches its API server, Prometheus and Loki through the tunnel. |
| **GPU & LLM serving** | Zero-touch GPU node onboarding (NVIDIA GPU Operator → vLLM → registered model), DCGM metrics in a native AI Factory UI, GPU sharing (time-slicing/MPS), a **local-first LLM router** with hosted fallbacks and per-tenant cost tracking, and AIPerf load tests. |
| **Observability** | Onboard an existing Prometheus/Loki/OpenSearch, or install a stack into a cluster in one click. Grafana dashboards render natively, and the agent can generate new ones from plain English. |
| **Network source of truth** | Nautobot as the source of truth (hosted per tenant or bring your own), bulk import of cabling/addressing/intent plans, NVIDIA Config Manager integration for config generation, a live LLDP topology map, and a click-to-SSH device terminal. |
| **Coding agent** | A terminal coding agent (`daalu` CLI), a VS Code extension and browser workspaces, all backed by a coding model on your GPU through an OpenAI-compatible gateway. |
| **Platform** | Multi-tenant with RBAC, Keycloak/OIDC SSO or local accounts, personal access tokens, workflows, morning briefings, Slack/email/webhook notifications, and AWS/GCP/Azure read-only integrations. |

---

## Quick start: on one machine (about 10 minutes)

You need `docker`, `kind`, `kubectl` and `helm`.

```bash
git clone https://github.com/kiwueke1/daalu.git
cd daalu
cp .env.example .env          # optional: set LLM_API_KEY / ANTHROPIC_API_KEY,
                              # or point LLM_LOCAL_BASE_URL at your own vLLM
make bootstrap-dev            # kind cluster + ingress + monitoring + Daalu
```

Then open **http://localhost:8080** and sign in with the admin email and
password the script prints at the end. Set `ADMIN_EMAIL` / `ADMIN_PASSWORD` to
choose them. The script also prints port-forward commands for Grafana and
Prometheus. `make bootstrap-dev` builds the images locally and loads them into
kind, so you don't need a registry.

For a breakable test workload that fires real alerts, see [`demo/`](demo/README.md).

### Running from source (for development)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

docker run -d --name daalu-postgres -e POSTGRES_DB=daalu_automation \
  -e POSTGRES_USER=daalu -e POSTGRES_PASSWORD=daalu_password \
  -p 5432:5432 postgres:16-alpine
docker run -d --name daalu-redis -p 6379:6379 redis:7-alpine

alembic upgrade head
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
(**Managed infrastructure → Add cluster**) prints the exact command:

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
├── migrations/                  # Alembic
├── docs/                        # architecture, guides, handbooks
└── tests/
```

## Documentation

- [docs/architecture.md](docs/architecture.md): how the pieces fit together
- [docs/system-walkthrough.md](docs/system-walkthrough.md): low-level design with code references
- [docs/getting-started.md](docs/getting-started.md): running the stack locally
- [docs/extending.md](docs/extending.md): adding a domain module
- [docs/cluster-tunnel-feature.md](docs/cluster-tunnel-feature.md): the WireGuard federation design
- [docs/sot-device-management/](docs/sot-device-management/): Nautobot source of truth and device onboarding
- [docs/book-customer/](docs/book-customer/README.md): the user handbook (features, integrations, troubleshooting)
- [docs/book-engineer/](docs/book-engineer/README.md): the contributor handbook (backend, frontend, federation, GPU, security, testing)
- [docs/runbooks/](docs/runbooks/README.md): runbooks linked from the shipped alert rules

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).
To report a vulnerability, see [SECURITY.md](SECURITY.md); please don't open a
public issue for it.

Third-party code vendored under `src/nv_config_manager_installer/`,
`components/nautobot/` and `deploy/charts/` keeps its upstream license (see
`src/nv_config_manager_installer/VENDORED.md`, `components/VENDORED.md` and
`deploy/charts/README.md`).
