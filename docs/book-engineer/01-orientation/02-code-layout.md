# 2. Code layout

The map of the source tree. Once you have a mental model of
where each thing lives, navigation gets a lot easier.

This chapter doesn't replace `tree` or your IDE's project
view — it gives you the *intent* of each directory.

---

## Top-level

```
daalu-automation/
├── src/daalu_automation/    # the Python application
├── frontend/                # Next.js operator UI
├── deploy/                  # Helm charts, k8s manifests, scripts
├── migrations/              # Alembic migrations
├── docs/                    # this manual + user guide + design docs
├── dev/                     # local-dev helpers (docker-compose, env templates)
├── tests/                   # pytest suite
├── scripts/                 # operational scripts (one-offs)
├── snapshots/               # test output (gitignored)
├── pyproject.toml           # Python project metadata + deps
├── alembic.ini              # migration tool config
├── Dockerfile               # backend image build
├── frontend/Dockerfile      # frontend image build
├── .github/workflows/       # CI definitions
└── README.md
```

The two image builds are decoupled — `Dockerfile` builds
`daalu-backend`, `frontend/Dockerfile` builds
`daalu-frontend`. CI workflow `build-images.yml` runs both
in parallel and pushes to GHCR.

---

## `src/daalu_automation/`

The Python package. Lay-out:

```
src/daalu_automation/
├── __init__.py              # __version__
├── cli.py                   # `daalu` CLI entrypoint
├── config.py                # pydantic-settings Settings
├── database.py              # SQLAlchemy engine, get_db, Base
├── observability.py         # structlog + Prometheus instrumentation
├── api/                     # FastAPI app
├── core/                    # cross-cutting libraries
├── models/                  # SQLAlchemy ORM models
├── modules/                 # domain modules (infra, alerts, …)
└── workers/                 # Celery worker setup + tasks
```

### `api/`

FastAPI application:

```
api/
├── __init__.py
├── main.py                  # create_app(), middlewares, router mounting
├── deps.py                  # FastAPI dependency functions (current_user, get_db, …)
└── routers/
    ├── agents.py
    ├── alert_chat.py
    ├── alerts.py
    ├── auth.py
    ├── billing.py
    ├── clusters.py
    ├── copilot.py
    ├── events.py
    ├── feedback.py          # new in 0007 migration
    ├── infra.py
    ├── integrations.py
    ├── onboarding.py
    ├── proposals.py
    ├── reports.py
    ├── sot_devices.py
    ├── sot_webhooks.py
    ├── workflows.py
    └── …
```

Each router is one HTTP surface. `main.py` mounts them under
`/api/v1/<name>`. `deps.py` is where shared dependencies
(authentication, DB session) live.

### `core/`

Cross-cutting libraries used across modules:

```
core/
├── auth.py                  # JWT, PAT, password hashing, scope checks
├── bootstrap.py             # ensure_default_tenant on startup
├── kube_tools.py            # the tool catalog for the LLM
├── llm_router.py            # routing decisions for inference
├── preferences.py           # default user prefs schema
├── billing/                 # SKU pricing, spend aggregation
├── reconciler/              # SoT reconciler (drift detection)
└── …
```

If you're looking for "where is the auth check," it's
`core/auth.py`. If you're looking for "where does the
Assistant decide what to do," it's `core/kube_tools.py` and
`core/llm_router.py`.

### `models/`

SQLAlchemy 2.0-style ORM models. One file per logical group:

```
models/
├── __init__.py              # re-exports
├── _mixins.py               # UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin
├── change_proposal.py
├── cluster_tunnel.py
├── event.py
├── feedback.py              # new in 0007
├── infra.py                 # Incident, Service
├── integration.py
├── personal_access_token.py # new in 0007
├── recommendation.py
├── tenant.py
├── user.py
└── …
```

Every domain model includes `TenantScopedMixin` which adds a
`tenant_id` column and a default query scope. See Chapter 6
for the schema and Chapter 57 for the tenant-isolation
contract.

### `modules/`

Domain-specific business logic. Each module corresponds to a
class of work Daalu does:

```
modules/
├── alerts/
├── infra/
├── integrations/            # adapter implementations
└── …
```

Modules are designed to be addable without touching the rest
of the application. See Chapter 62 ("Adding a new domain
module") for the contract.

### `workers/`

Celery workers:

```
workers/
├── __init__.py
├── celery_app.py            # the Celery Celery() instance + config
├── executor.py              # daalu-executor entrypoint
├── beat_schedule.py         # cron-like schedule for beat
└── tasks/
    ├── alerts.py
    ├── reconcile.py
    └── …
```

`celery_app.py` is the entrypoint that both `daalu worker` and
`daalu executor` import. The split between the two is by
**queue subscription** — see Chapter 7.

---

## `frontend/`

Next.js 14 with the App Router:

```
frontend/
├── package.json
├── next.config.js
├── tsconfig.json
├── Dockerfile
├── app/                     # routes (App Router)
│   ├── layout.tsx           # root layout
│   ├── page.tsx             # home page
│   ├── login/
│   ├── onboarding/
│   ├── alerts/
│   ├── operations/
│   ├── agents/
│   ├── automations/
│   ├── billing/
│   ├── reports/
│   ├── settings/
│   ├── help/
│   ├── managed-infra/       # tabs: clusters, network, onboarding, integrations
│   ├── investigations/ runbooks/ autonomy/ maturity/ evals/ audit/
│   ├── integrations/        # redirect → /managed-infra?tab=integrations
│   ├── devices/             # redirect → /operations?tab=devices (+ [id] detail)
│   ├── clusters/            # redirect → /managed-infra?tab=clusters (+ [slug] console)
│   ├── proposals/           # redirect → /operations?tab=proposals (+ [id] detail)
│   ├── …
│   ├── globals.css          # theme tokens, motion, focus ring
│   └── providers.tsx        # React Query, theme, toasts, auth
├── components/              # shared UI components
│   ├── layout/              # app-shell, sidebar, header, right-panel, assistant-sheet
│   ├── ui/                  # Button, Dialog, TabBar, Accordion, ConfirmButton, LoadState, toast
│   └── …                    # per-domain folders (alerts/, aiops/, managed-infra/, …)
├── lib/                     # client utilities
│   ├── api.ts               # API client (fetch wrappers)
│   ├── auth.tsx             # AuthProvider / useAuth
│   ├── errors.ts            # errorText(): every error shown to people
│   └── …
├── DESIGN.md                # UI rules every screen follows
└── e2e/                     # Playwright screenshot tests
```

Each route directory has a `page.tsx` (the route's main
component) and optionally `loading.tsx`, `error.tsx`,
`layout.tsx`.

---

## `deploy/`

```
deploy/
├── helm/
│   ├── daalu-automation/    # the cloud-side chart
│   │   ├── Chart.yaml
│   │   ├── values.yaml
│   │   └── templates/
│   │       ├── api.yaml
│   │       ├── workers.yaml
│   │       ├── frontend.yaml
│   │       ├── postgres.yaml
│   │       ├── redis.yaml
│   │       ├── wireguard-hub.yaml
│   │       ├── networkpolicy.yaml
│   │       └── …
│   ├── daalu-edge/          # customer-side federation
│   └── daalu-vllm/          # customer-side inference
├── k8s/                     # raw manifests
│   ├── monitoring/          # kube-prom-stack values, ServiceMonitors
│   ├── gpu/                 # GPU operator / device plugin
│   ├── guardrails/          # policy webhooks
│   └── …
├── argocd/
│   └── applications/        # ArgoCD Application manifests
├── scripts/
│   ├── install.sh           # the one-shot deploy script
│   ├── deploy-observability.sh
│   ├── deploy-gpu.sh
│   └── …
├── bootstrap-prod.sh        # full cluster bootstrap
├── edge/                    # daalu-edge image source
└── wg-hub/                  # WireGuard hub image source
```

The deploy story has more moving parts than it'd like. Part
VIII unpacks why and what to do about it.

---

## `migrations/`

Alembic. Numbered revisions:

```
migrations/
├── env.py
├── versions/
│   ├── 0001_initial.py
│   ├── 0002_…
│   ├── 0006_billing.py
│   └── 0007_user_settings_pats_feedback.py
└── alembic.ini → ../alembic.ini
```

Each migration is autonomous (no skipping). See Chapter 50 for
production migration practices.

---

## `tests/`

```
tests/
├── conftest.py              # pytest fixtures (db, client, etc.)
├── unit/                    # fast, isolated
├── integration/             # full DB, real Celery, slower
└── e2e/                     # against a deployed instance
```

The split matters because CI runs them differently — see
Chapter 60.

---

## `dev/`

Stuff useful only for local development:

- `docker-compose.yaml` — Postgres, Redis, Loki, Grafana on
  your laptop.
- `.env.example` — environment variable template.
- `seeds/` — JSON snapshots used by `daalu seed-demo`.

---

## `scripts/`

One-off operational scripts:

- `scripts/rotate-secrets.sh`
- `scripts/run-migration-prod.sh`
- `scripts/dump-tenant.py`

If you find yourself running one of these often, promote it
into the `daalu` CLI in `cli.py` instead.

---

## What's next

Chapter 3 — Tech stack and the rationale behind each choice.
