# 28. Daalu Private

Customer-facing chapter: book-customer §44.

This chapter is the engineer's view of the tier. Where the
edge-side agent loop runs, how the hub keeps from running the same
agent twice for an edge tenant, what's persisted where, and what's
still scaffolding versus production.

---

## Two independent switches

| Tenant flag (column on `tenants`) | Effect |
|---|---|
| `is_private` | Master toggle. UI shows "Private" badge, billing rate changes, eligibility-gated features unlock. |
| `edge_agents_enabled` | Agent loop runs in the customer's cluster (via `daalu-edge` chart). Hub-side agent host skips this tenant. |
| `private_db_url` + `private_db_token_hash` | Tenant-scoped tables live in the customer's Postgres. (Scaffolding only — see below.) |

A tenant can be `is_private=true, edge_agents_enabled=true,
private_db_url=NULL` — "Private (edge agents)" — that's a real
deployable shape. The `private_db_url` switch is the bigger Phase 3
project; today it's a column and a chokepoint with a clear TODO.

---

## Edge-side agent loop

```
   ┌──────────────────────────┐                ┌──────────────────────┐
   │   Daalu hub              │                │  Customer cluster    │
   │                          │                │                      │
   │  ┌──────────────────┐    │   long-poll    │  ┌────────────────┐  │
   │  │ daalu-agents     │    │  ◀──────────── │  │ daalu-edge-    │  │
   │  │ (skips edge      │    │                │  │ agents pod     │  │
   │  │  tenants)        │    │                │  │ (mode=edge)    │  │
   │  └──────────────────┘    │                │  └────────────────┘  │
   │                          │                │           │           │
   │  POST /internal/         │  ◀── results ──┤           │ tools     │
   │       agent-results      │                │           ▼           │
   │  GET  /internal/         │                │      ┌─────────┐     │
   │       agent-tasks        │                │      │ in-cluster │   │
   │                          │                │      │ Prom/Loki │   │
   │                          │                │      │ kubectl   │   │
   │                          │                │      └─────────┘     │
   └──────────────────────────┘                └──────────────────────┘
```

The hub's agent host (``workers/agent_runner.py``) gates every
event on ``core.agents._should_handle_on_hub(event.tenant_id)``;
that helper looks up ``tenant.edge_agents_enabled`` (with a 30 s
cache) and returns False for edge tenants. Result: the hub
processes events for Standard tenants only.

The edge pod runs the same backend image with
``daalu agents --mode=edge``. The mode flag dispatches to
``workers/edge_agent_runner.run_edge_loop`` which long-polls
``/api/v1/internal/agent-tasks`` and POSTs synthesised results
back via ``/api/v1/internal/agent-results``.

Two endpoints (in ``api/routers/internal_agents.py``):

* `GET /api/v1/internal/agent-tasks` — service-token auth, returns
  pending tasks for the *caller's* tenant. Currently returns `[]`;
  the hub-side outbox that populates the queue is Phase 2.
* `POST /api/v1/internal/agent-results` — service-token auth,
  records a synthesised result. Schema deliberately excludes raw
  telemetry — only the agent's English/JSON answer is accepted.

Auth: per-tenant **service token** issued at Private-tier
provisioning, distinct from user PATs and from inter-service tokens
between hub services. Same JWT format (`core/service_tokens.py`),
different `purpose` value.

---

## What never reaches the hub in edge-agents mode

For an `edge_agents_enabled` tenant, the hub:

* never sees the raw Prometheus query result
* never sees the log lines pulled from Loki
* never sees the device config the agent inspected
* never sees the prompt the agent sent to its LLM (SOVEREIGN or
  daalu-hosted)

The hub sees:

* the *fact* that an event triggered an agent run (tenant + agent + ts)
* the agent's synthesised summary text
* the agent's structured artefact (alert enrichment JSON, briefing
  summary, draft proposal diff)
* token-usage counts for billing

That's the contract enforced by the `/api/v1/internal/agent-results`
schema. A future protocol audit would specifically check that the
schema doesn't drift to include raw fields.

---

## Customer-managed Postgres

The chokepoint is `core/tenant_db.py::get_tenant_session(tenant)`.
Today it yields the hub session for everyone (with a warning log
when a Private-flagged tenant hits it). The Phase 3 implementation
plan:

1. Resolve the cleartext DB token from a K8s Secret keyed by
   tenant (parallel to the SOVEREIGN-token mount pattern in
   `core/llm.py::_resolve_sovereign_token`).
2. Build the URL and `create_async_engine` with appropriate pool
   size. Cache by URL.
3. Yield a session against that engine.

Major callsite migration work (Phase 3):

* Every call that does `async with AsyncSessionLocal() as db: ...`
  *for tenant-scoped tables* needs to switch to
  `async with get_tenant_session(tenant): ...`.
* Account / billing tables stay on the hub session — they're
  cross-tenant.
* Migration tooling needs to split `migrations/versions/` by table
  domain so the customer's DB only runs the tenant-data subset.

The chokepoint exists *now* so call-site changes can happen
incrementally, ahead of the resolver implementation.

---

## Wire-up summary

| File | What it owns |
|---|---|
| `migrations/versions/0009_daalu_private.py` | The four new tenant columns. |
| `src/daalu_automation/models/tenant.py` | ORM mapping for the columns. |
| `src/daalu_automation/core/tenant_db.py` | Per-tenant session chokepoint (scaffolding). |
| `src/daalu_automation/core/agents.py::_should_handle_on_hub` | Hub-side gate that skips edge tenants. |
| `src/daalu_automation/workers/agent_runner.py` | `--mode=edge` dispatches to the edge loop. |
| `src/daalu_automation/workers/edge_agent_runner.py` | Long-poll loop on the customer side. |
| `src/daalu_automation/api/routers/internal_agents.py` | Hub-side endpoints the edge pod talks to. |
| `deploy/helm/daalu-edge/templates/agents.yaml` | The edge-agents Deployment template. |
| `deploy/helm/daalu-edge/values.yaml` | `agents.enabled` toggle + config keys. |

---

## What's done vs what's scaffolding

**Done end-to-end (deployable, exercises the contract):**

* Tenant flag columns + migration
* Hub-side gating helper
* `daalu agents --mode=edge` CLI + runner dispatch
* `daalu-edge` chart's `agents.yaml` template
* Internal API endpoints with the right auth + schema
* Edge polling loop with backoff, auth headers, result shape

**Scaffolding (the seam works, the brain is TODO):**

* Hub-side outbox that populates `/agent-tasks` with real events.
  Currently returns `[]`.
* Per-task processors on the edge that turn an `alert_explain`
  task into a real LLM-driven enrichment. Currently returns a
  stub summary.
* Per-tenant secret rotation for the edge-agent service token.
* The DB router (`core/tenant_db.py`) — see above.
* Distributed-transaction handling for the customer-Postgres path.

The seams are deliberate: each scaffolding piece has exactly one
location to grow next, and the rest of the codebase already calls
through the right chokepoint.

---

## Known gaps for production-grade

* [ ] Hub-side outbox: when an event arrives for an
  edge_agents_enabled tenant, write a row to an `agent_tasks`
  table (Phase 2) and surface it on `/agent-tasks`.
* [ ] Per-task processor implementations on the edge side.
* [ ] `daalu migrate-tenant <id>` CLI for customer-Postgres
  schema deployment.
* [ ] Cross-DB outbox + sweep tasks (eventual consistency between
  hub and tenant DB).
* [ ] Pen-test the `/api/v1/internal/*` boundary specifically.
* [ ] Cost-model validation: does the 3-5× premium clear the
  added operational overhead?
* [ ] SOC 2 control mapping for the new data flow.

---

## See also

* Chapter 34 (customer book) — daalu-edge install (the chart that
  now hosts the edge agents)
* Chapter 39 (customer book) — Daalu Private buyer-facing
* Chapter 18 (customer book) — Change proposals (the agent's only
  path to changing customer infrastructure, regardless of tier)
