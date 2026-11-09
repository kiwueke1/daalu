# 1. The platform in one page

This is the single page you should read first if you're new to
the codebase. It's not exhaustive; it's a fast scaffolding that
makes the rest of the manual easier to navigate.

If anything below is wrong, send a patch. The manual rots if
nobody updates it.

---

## The big picture

Daalu Automation is a multi-tenant SaaS platform with three
physical components:

1. **The Daalu cloud** — what we operate.
2. **Daalu Edge** — what runs in each customer's Kubernetes
   cluster.
3. **Daalu vLLM** — what runs on each customer's GPU node.

The Daalu cloud is a Kubernetes-hosted application running on
an operator Kubernetes cluster (a single-node k3s is enough;
point `$KUBECONFIG` at it). Edge and vLLM are Helm charts customers
install themselves.

---

## Inside the Daalu cloud

The cloud-side architecture:

```
                    ┌──────────────────────────┐
                    │  operator k3s cluster    │
                    │                          │
   internet  ──→  traefik  ──→  daalu-frontend  (Next.js)
                                      │
                                      └──→  daalu-api (FastAPI)
                                              │
              ┌───────────────┬───────────────┼───────────────┬──────────┐
              ↓               ↓               ↓               ↓          ↓
        daalu-postgres   daalu-redis     daalu-worker    daalu-beat   daalu-executor
         (StatefulSet)    (cache+broker)   (Celery)      (scheduler)  (action runner)
                                              │
                                              └─→ daalu-agents (LLM-driven jobs)

   federation:
                                      ┌─→ daalu-wireguard-hub  ──→ tunnels to customer clusters
                                      │
                                      └─→ kube-prometheus-stack, loki, promtail
```

What each pod does:

- **daalu-api** — the FastAPI HTTP server. Every read and most
  writes go through here. Source in `src/daalu_automation/api/`.
- **daalu-frontend** — Next.js 14 (App Router) operator UI.
  Talks only to daalu-api. Source in `frontend/`.
- **daalu-postgres** — primary database. Per-tenant data.
  Single StatefulSet.
- **daalu-redis** — both a cache (for hot data) and a Celery
  broker.
- **daalu-worker** — Celery worker pool. Runs the default
  task queue. Source in `src/daalu_automation/workers/`.
- **daalu-beat** — Celery beat scheduler. Single replica
  enforced by chart.
- **daalu-executor** — A specialized Celery worker with the
  JWT scope `executor` that's the only identity allowed to
  apply approved change proposals. See Chapter 17.
- **daalu-agents** — Long-running LLM-driven tasks (the
  alert-explainer, briefing agent, etc.).
- **daalu-wireguard-hub** — The WireGuard server that
  terminates per-tenant federation tunnels. The single point
  of network ingress from customer environments.

Observability sits in the `monitoring` namespace:

- **kube-prometheus-stack** — Prometheus, Grafana, Alertmanager,
  kube-state-metrics, node-exporter.
- **loki** — log aggregation.
- **promtail** — log shipping from every pod.

---

## How a single request flows

A `GET /api/v1/alerts` from a logged-in operator:

1. Traefik routes `ops.example.com` to the `daalu-api` Service.
2. The Service forwards to one of N `daalu-api` pods (default
   3 replicas).
3. Inside the pod, FastAPI dispatches to
   `src/daalu_automation/api/routers/alerts.py`.
4. The route handler calls `current_user` (from `api/deps.py`)
   which decodes the JWT or PAT and loads the User row.
5. The handler issues a SQLAlchemy query scoped to the user's
   `tenant_id`.
6. Response is serialized as JSON and returned.

Latency goal: p99 < 250 ms for cached endpoints, < 1 s for
the broad inventory queries.

---

## How a state-changing request flows

Most writes follow this shape:

1. API endpoint receives the write.
2. Inside the handler, it writes a row to `change_proposals`
   with status `proposed`.
3. The handler returns 202 Accepted with the proposal ID.
4. A human (or admin-pre-approved automation) calls
   `POST /api/v1/proposals/<id>/approve`.
5. The approval handler transitions the proposal to
   `approved` and writes a Celery task to the `executor`
   queue.
6. **daalu-executor** picks up the task, calls the relevant
   tool function, and updates the proposal to `applied` (or
   `error`).

The executor is the only identity in the platform that runs
write tools. Its JWT carries scope `executor`; the gate is in
`src/daalu_automation/core/auth.py` and is enforced by tool
implementations via `core.kube_tools._require_executor_scope()`.

---

## How AI inference flows

The operator UI's Assistant panel sends each turn to
`POST /api/v1/copilot/chat`. The handler:

1. Loads conversation history from Postgres.
2. Determines available tools based on the page context and
   the user's permissions.
3. Calls the **LLM router** (in `src/daalu_automation/core/
   llm_router.py`). The router decides: local pod or cloud
   provider.
4. Receives a response (possibly with tool calls).
5. If the model wanted to call a write tool, the router
   intercepts and writes a change proposal instead.
6. Returns the response to the frontend.

The local pod lives in the customer's cluster, reached
through the federation tunnel.

---

## How federation works at runtime

For a customer with cluster federation enabled:

1. Their `daalu-edge` pod opens a WireGuard tunnel to
   `daalu-wireguard-hub`.
2. The hub assigns an IP in the tenant's `/24` subnet
   (default `10.200.0.0/24` per tenant).
3. When the operator app needs to reach a service in that
   cluster, it calls the cluster-internal address through
   the tunnel, e.g.
   `http://kube-prom-stack-kube-prome-prometheus.monitoring.svc:9090`.
4. The hub's routing table forwards via WireGuard; the edge
   pod NATs to the in-cluster address.

This is what makes "the Prometheus inside your cluster is
queryable by Daalu" work without any inbound port.

---

## Where the data lives

- **Postgres** — `daalu-postgres-0` StatefulSet. Holds
  tenants, users, alerts, events, devices, change proposals,
  audit logs, PATs, feedback. Back it up with volume
  snapshots and/or a nightly pg_dump.
- **Redis** — cache and Celery broker. Ephemeral.
- **Prometheus** — metrics for the operator app's own
  observability. Retention 30 days.
- **Loki** — logs from every pod. Retention 14 days.

Customer data (their pod logs, their metrics) lives in their
cluster's Prometheus/Loki. Daalu only proxies queries; it
doesn't snapshot.

---

## The platform's own observability

We dogfood. Daalu's operator app:

- Scrapes its own /metrics via a ServiceMonitor.
- Ships pod logs to its own Loki.
- Fires its own alerts when something's wrong.
- The on-call uses the operator app itself to investigate
  issues with the operator app.

The chicken-and-egg case ("Daalu is down, how do I see why?")
is handled by a small "platform health" dashboard hosted
outside the cluster on Grafana Cloud. See Chapter 40.

---

## The repo at a glance

Top-level:

- `src/daalu_automation/` — the Python application.
- `frontend/` — the Next.js app.
- `deploy/helm/daalu-automation/` — the Helm chart that
  deploys the operator app.
- `deploy/helm/daalu-edge/` — the customer-side Helm chart.
- `deploy/helm/daalu-vllm/` — the customer-side vLLM chart.
- `deploy/k8s/` — supporting raw manifests (monitoring config,
  GPU, network policies).
- `deploy/argocd/` — ArgoCD application manifests.
- `deploy/scripts/install.sh` — manual rollout script.
- `migrations/` — Alembic migration scripts.
- `docs/` — this manual and the user guide.
- `dev/` — local-dev helpers.
- `tests/` — pytest test suite.
- `scripts/` — assorted operational scripts.
- `snapshots/` — output of test runs (not committed).

Chapter 2 walks the source tree in detail. Chapter 3 covers
why we picked each piece of tech.

---

## What's next

Chapter 2 — Code layout. Chapter 3 — Tech stack rationale.
After Part I, Part II goes deep on the backend.
