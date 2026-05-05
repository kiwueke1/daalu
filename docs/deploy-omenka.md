# Omenka — Codebase Walkthrough & Deployment Guide

This document is a deep walkthrough of the two coupled projects living under `/home/kez/Documents/omenka/`:

- **`bulletproof_bt/`** — a deterministic, institutional-grade Python backtesting engine
- **`invariance_research/`** — a Next.js + TypeScript SaaS that wraps `bulletproof_bt` and sells strategy-validation reports

The two are tightly coupled — the SaaS literally imports the engine.

The final section covers Kubernetes packaging and public exposure for both.

---

## 1. The big picture

```
┌──────────────────────────────────────────────────────────────────┐
│                      invariance_research                         │
│  Next.js 15 (App Router) + TS + React 19 + Tailwind              │
│  Node backend (same process) + SQLite + Stripe + Auth.js         │
│  Spawns Python subprocess  ──────────────────────┐               │
└──────────────────────────────────────────────────┼───────────────┘
                                                   │ JSON over stdio
                                                   ▼
                                ┌──────────────────────────────────┐
                                │         bulletproof_bt           │
                                │  Python 3.11+ deterministic      │
                                │  backtesting engine              │
                                │  • CLI scripts                   │
                                │  • Public API: bt.run_*          │
                                │  • Optional FastAPI dashboard    │
                                │  • Optional research_daemon      │
                                │  • Live Bybit adapter (REST/WS)  │
                                └──────────────────────────────────┘
```

A typical request inside `invariance_research`:

1. User uploads a `.csv` or `.zip` of trade artifacts → `/api/uploads/inspect` parses, validates, and scores eligibility.
2. User confirms → `/api/analyses` POST inserts an `analysis_jobs` row in SQLite.
3. The analysis worker (embedded loop or split process) claims the row, **spawns `python3 scripts/run_bulletproof_engine.py`**, pipes the parsed artifact in as JSON, and waits.
4. The Python bridge calls `bt.run_analysis_from_parsed_artifact(...)` — this is the public API of the `bulletproof_bt` package (pinned to `v0.2.2` from GitHub).
5. The engine returns a JSON blob of figures (overview, distribution, monte-carlo, execution, regimes, ruin, stability, report). The Node side adapts those into product contracts and persists them.
6. The frontend renders ECharts visualisations over those persisted records; the user can also enqueue an export (JSON/Markdown/PDF) handled by the export worker.

Stripe handles billing and entitlements gate which diagnostics are available. Admin-only routes (allow-listed by env vars) cover ops dashboards: jobs, exports, webhooks, accounts, health, maintenance.

---

## 2. `bulletproof_bt` — the engine

### 2.1 What it is

A deterministic, event-driven, single-strategy quantitative backtester aimed at institutional research:

- Same data + same config → identical outputs (bit-for-bit reproducibility)
- Multi-asset: crypto (24/7), forex (24×5), equities (session-based), futures
- Every run produces a versioned, auditable artifact bundle
- Strict invariants between strategy ↔ risk ↔ execution ↔ portfolio ↔ feed ↔ benchmark ↔ artifacts
- **Out of scope (V1):** multi-strategy blending, portfolio allocation, tick/order-book simulation, web dashboards as production surfaces

### 2.2 Layout

```
bulletproof_bt/
├── src/bt/                   # ~315 Python files — the engine itself
├── orchestrator/             # FastAPI dashboard, research daemon, pipeline runner
├── scripts/                  # 20 CLI entry points
├── configs/                  # Layered YAML packs (engine, fees, slippage, exec, experiments)
├── tests/                    # ~170 deterministic / contract / regression tests
├── docs/                     # ~74 markdown contracts and runbooks
├── examples/reference_artifacts/  # Reference outputs used by regression tests
├── research/                 # Hypotheses and audit notes
├── data/                     # Sample datasets
└── debug/                    # Debug helpers
```

### 2.3 Core modules (`src/bt/`)

| Module | Responsibility |
|---|---|
| `core/` | Engine loop (`engine.py`), config resolution, clocks, reason codes |
| `data/` | Dataset loading, validation, resampling, streaming feeds, market rules |
| `execution/` | Execution profiles, pricing, slippage, spread, commission, fees |
| `risk/` | Position sizing, margin modeling, stop handling |
| `portfolio/` | Cash management, positions, liquidation, accounting |
| `metrics/` | Performance, attribution, reconciliation, R-metrics |
| `logging/` | Artifact writers, trade schemas, decision traces |
| `benchmarks/` | `buy_hold` / `flat` / `baseline` modes, comparison metrics |
| `instruments/` | Asset-class abstractions (forex/equity/crypto/futures specs) |
| `strategy/` | Strategy base classes, built-ins, context views |
| `indicators/` | 45+ streaming indicators (MA, RSI, ATR, Bollinger, Supertrend, …) |
| `orders/` | Order side validation/resolution |
| `universe/` | Universe filtering (history, volume, lag) |
| `analysis/` | Overview payload + feature extraction |
| `audit/` | Determinism + signal/order/fill/position/portfolio audits |
| `validation/` | Config completeness + schema versions |
| `experiments/` | Hypothesis & experiment configuration |
| `features/` | Online state layer, price-action features |
| `hypotheses/` | Hypothesis registry & base types |
| `contracts/` | Schema versions across all artifact types |
| `saas/` | `StrategyRobustnessLabService` — used by the SaaS layer |
| `exec/` | Live execution: Bybit adapter (REST + WS), simulated, paper, shadow |
| `benchmark/` | Legacy benchmark module |

Public API (`bt/__init__.py`):

```python
from bt import (
    run_backtest,
    run_grid,
    run_analysis_from_parsed_artifact,   # ← what invariance_research calls
    __version__,
)
```

`src/bt/api.py` (~22 KB) is the thin facade. The heavy lifting is `src/bt/core/engine.py` (~27 KB) and `src/bt/saas/service.py` (~177 KB — the SaaS feature surface).

### 2.4 Running it

| Surface | Entry | Purpose |
|---|---|---|
| Library | `from bt import run_backtest, run_grid, run_analysis_from_parsed_artifact` | Programmatic use |
| CLI | `scripts/run_backtest.py`, `run_experiment_grid.py`, `run_parallel_grid.py`, … (20 scripts) | Single runs, parameter sweeps, parallel grids, post-run analysis, MFE diagnostics, dataset extraction |
| Live exec CLI | `scripts/run_exec_bybit_demo.py`, `run_exec_bybit_live.py`, `run_exec_paper.py`, `run_exec_shadow.py`, `run_exec_doctor.py` | Connects to Bybit demo/live/paper/shadow modes |
| Dashboard | `python orchestrator/run_dashboard.py --db <path> [--host …] [--port 8765]` | Optional FastAPI + Jinja2 + SQLite UI for browsing runs |
| Daemon | `python orchestrator/research_daemon.py --db <path> --config daemon.yaml` | Polls SQLite queue and runs hypothesis/experiment jobs |
| Pipeline | `python orchestrator/run_experiment_pipeline.py --hypothesis <path> --name <n> --max-workers N` | End-to-end multi-worker pipeline |

### 2.5 Configuration

YAML, deeply layered — base → fees → slippage → exec → experiment overrides → local config. Notable packs in `configs/`:

- `engine.yaml`, `fees.yaml`, `slippage.yaml`, `exec.yaml`
- `packs/crypto_v1.yaml`, `packs/fx_trad_v1.yaml`
- `exec/bybit_demo.yaml`, `exec/bybit_live.yaml`, `exec/bybit_live_canary.yaml`, `exec/paper_simulated.yaml`, `exec/shadow_simulated.yaml`
- `experiments/h1_volfloor_donchian.yaml`, `experiments/h1_volfloor_emapullback.yaml`
- `examples/{safe_client,strict_research,fx_safe_client,equity_safe_client}.yaml`

### 2.6 Dependencies

Core: `numpy`, `pandas`, `pyarrow`, `matplotlib`, `pyyaml`. Optional dashboard: `fastapi`, `uvicorn`, `jinja2`. Dev: `pytest`, `ruff`, `mypy`. **No** ORM, no distributed framework, no message broker — orchestration is multiprocessing + a small SQLite queue.

### 2.7 Run artifacts

Each run drops a directory like:

```
run_xxx/
  config_used.yaml
  performance.json
  equity.csv
  trades.csv
  fills.jsonl
  decisions.jsonl
  performance_by_bucket.csv
  cost_breakdown.json
  summary.txt
  run_manifest.json
  run_status.json
  benchmark_*           # if benchmarks enabled
```

These are exactly the files the SaaS layer parses and renders.

### 2.8 What does **not** ship

No Dockerfile, no Kubernetes manifests, no docker-compose, no CI/CD config. Today it's a Python virtualenv + scripts, optionally fronted by a local FastAPI dashboard.

---

## 3. `invariance_research` — the SaaS

### 3.1 What it is

A **monolithic Next.js 15 (App Router) app** that delivers paid, async, execution-aware strategy diagnostics. A user uploads trade artifacts; the app validates eligibility, charges per-plan, runs `bulletproof_bt` via a Python subprocess, persists the result, renders interactive diagnostic pages, and lets them export the result as JSON, Markdown, or PDF.

### 3.2 Stack

- **Frontend:** Next.js 15.1.0 + React 19 + TypeScript 5.7 + Tailwind 3.4 + ECharts
- **Backend:** Same Next.js process — server components, route handlers, server actions
- **Auth:** `next-auth` v5 beta (credentials, scrypt-hashed passwords, JWT sessions)
- **DB:** SQLite via Node 22's built-in `node:sqlite` (`DatabaseSync`), file at `.data/invariance.sqlite`
- **Storage:** Local filesystem under `.data/storage` (no S3 yet)
- **Billing:** Stripe (checkout + portal + webhooks)
- **Job queue:** SQLite-backed, polled by an embedded loop **or** by split `tsx`-run worker processes
- **Engine bridge:** Spawns `python3 scripts/run_bulletproof_engine.py`, JSON over stdio
- **Python dep:** `bulletproof_bt @ git+https://github.com/Chinedum-iwueke/bulletproof_bt.git@v0.2.2`

### 3.3 Routes

**Public (marketing + content):** `/`, `/about/[slug]`, `/pricing`, `/methodology`, `/research/[slug]`, `/research-standards`, `/research-desk`, `/strategy-validation`, `/robustness-lab`, `/lab`, `/contact`, `/account`, `/docs/lab`, `/ui-kit`.

**Authenticated app:**

- `/app`, `/app/analyses`, `/app/new-analysis`, `/app/billing`, `/app/upgrade`, `/app/settings`
- `/app/analyses/[id]/{overview,distribution,monte-carlo,execution,regimes,ruin,stability,report}`

**Admin (env-allow-listed):** `/app/admin/{jobs,webhooks,exports,accounts,health,maintenance,publications,waitlist}`.

**Auth:** `/(auth)/login`, `/(auth)/signup`.

### 3.4 API endpoints (selected)

User:
`POST /api/uploads/inspect`, `GET|POST /api/analyses`, `GET|PUT /api/analyses/[id]`, `GET /api/analyses/[id]/status`, `POST /api/analyses/[id]/retry`, `POST /api/analyses/[id]/exports`, `GET /api/exports/[id]`, `GET /api/exports/[id]/download`, `GET /api/usage`, `POST /api/billing/checkout`, `POST /api/billing/portal`.

Public:
`POST /api/auth/[...nextauth]`, `POST /api/auth/register`, `POST /api/waitlist`, `GET /api/health`, `GET /api/benchmark-library/manifest`, `GET /api/benchmark-library/health`, `GET /api/publications/assets/[...assetPath]`.

Admin:
`POST /api/admin/jobs/[id]/retry`, `POST /api/admin/exports/[id]/retry`, `POST /api/admin/webhooks/[id]/reprocess`, `POST /api/admin/maintenance/[action]`, plus publications and waitlist CRUD.

Webhook:
`POST /api/webhooks/stripe` (signature-verified).

### 3.5 Backend layout (`src/lib/server/`)

- `persistence/` — SQLite connection + 6 schema migrations (core tables, exports, heartbeats, benchmarks, runtime config, publications, waitlist)
- `repositories/` — analysis, artifact, job, export, export-job, webhook-event, worker-heartbeat (no ORM, raw SQL)
- `auth/` — NextAuth credential provider + session helpers
- `accounts/` — account lifecycle
- `services/` — `analysis-service`, `analysis-job-runner`, `upload-intake-service`, `analysis-normalizer`, `analysis-view-service`
- `ingestion/` — CSV + ZIP parsers, Zod schemas, eligibility classifier, semantic validators
- `engine/` — `bulletproof-client` (spawns Python subprocess), `bulletproof-runner`, types
- `adapters/bulletproof/` — maps engine output → product contracts (`map-analysis`, `map-overview`, `map-monte-carlo`, `map-report`, `map-engine-analysis-record`)
- `exports/` — service + renderer (JSON/MD/PDF) + models
- `workers/` — `analysis-worker`, `export-worker`, generic `worker-runtime` loop with heartbeat
- `entitlements/` — plan matrix (explorer / professional / research_lab / advisory), policy, monthly usage
- `billing/` — Stripe client, checkout, portal, webhook handler, billing config
- `admin/` — guards + jobs/exports/webhooks/accounts/health/maintenance services
- `ops/` — health checks, logger, startup validation (probes Python + bridge + engine)
- `storage/` — local FS adapter
- `waitlist/` — lead capture

### 3.6 Workers and the Python bridge

Two run modes, switched by `INVARIANCE_EMBEDDED_WORKERS`:

- `true` (default) — workers live in the Next.js process. Fine for dev; fails to scale.
- `false` — workers run as separate `tsx`-launched processes:
  - `npm run worker:analysis` → `scripts/run-analysis-worker.ts`
  - `npm run worker:export` → `scripts/run-export-worker.ts`

Both modes share the SQLite file and storage root, so on a single host they Just Work; across hosts you have to migrate to a shared DB and shared object store (the docs call this out explicitly as a deployment blocker).

The engine bridge (`scripts/run_bulletproof_engine.py`, ~19 KB) is the only Python in this repo. It:

1. Reads JSON payload from stdin
2. Imports `bt`
3. Validates against the runtime model seam
4. Calls `bt.run_analysis_from_parsed_artifact(parsed_artifact, config?)`
5. Emits JSON result on stdout
6. Surfaces structured error codes on failure

Probed on startup via a `--probe` flag and again via `/api/health`.

### 3.7 npm scripts and key env vars

```jsonc
{
  "dev": "next dev",
  "build": "next build",
  "start": "next start",
  "lint": "next lint",
  "worker:analysis": "tsx scripts/run-analysis-worker.ts",
  "worker:export":   "tsx scripts/run-export-worker.ts",
  "usage:recalculate": "tsx scripts/recalculate-usage.ts"
}
```

Selected env vars:

```
INVARIANCE_DB_PATH                  default .data/invariance.sqlite
INVARIANCE_STORAGE_ROOT             default .data/storage
INVARIANCE_EMBEDDED_WORKERS         true|false
INVARIANCE_ANALYSIS_WORKER_POLL_MS  poll interval
INVARIANCE_EXPORT_WORKER_POLL_MS    poll interval
INVARIANCE_WORKER_STALE_MS          heartbeat freshness
INVARIANCE_PYTHON_BIN               python3
INVARIANCE_BULLETPROOF_BRIDGE_SCRIPT  script path override
INVARIANCE_ENGINE_TIMEOUT_MS        default 120000
INVARIANCE_BENCHMARK_LIBRARY_ROOT
STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET
STRIPE_PRICE_PROFESSIONAL / STRIPE_PRICE_RESEARCH_LAB
ADMIN_EMAILS / ADMIN_USER_IDS       allow-list
APP_URL                             checkout/portal return URL
```

### 3.8 Production-readiness gaps (from the repo's own docs)

`docs/repo-aware-architecture-and-deployment-report-2026-04-17.md` calls these out:

- **SQLite is a hard ceiling** — needs migration to managed Postgres (Neon/Supabase/RDS).
- **Local filesystem coupling** — needs S3/R2/Backblaze + an object-storage adapter.
- **No Dockerfile / k8s / IaC** in repo — must be added.
- Web tier could go to Vercel; workers must be containerised separately because Vercel can't run long-lived poll loops or invoke Python subprocesses reliably.

---

## 4. Deploying to Kubernetes

This section covers Docker packaging and a public, TLS-terminated Kubernetes deployment for both projects. Everything below is a blueprint — adapt names, registries, and resource sizes to your actual cluster.

### 4.1 Architectural decisions before you write any YAML

Before packaging anything, three changes to `invariance_research` are effectively required for a real cluster (the repo's own docs flag the first two; the third becomes critical the moment you run more than one worker):

1. **Migrate persistence from SQLite (`node:sqlite`) to managed Postgres.** SQLite holds the job queue, Stripe webhook log, analyses, exports, accounts, etc. The current code uses `DatabaseSync`; a single replica per database file is the hard limit. For a real cluster you need either:
   - Postgres + a thin repository-layer rewrite (preferred), or
   - A `ReadWriteOnce` PVC with **exactly one replica** of every component that opens the DB (web, analysis worker, export worker) — fragile, defeats HPA, only buys you time.
2. **Move uploaded artifacts and exports off local disk to S3-compatible object storage.** The current `storage/` adapter reads/writes `.data/storage/...`. Replace with an S3 adapter (R2, MinIO, AWS S3) or you'll need a `ReadWriteMany` PVC shared between web + workers, which most clusters don't offer cheaply.
3. **Harden the job queue beyond polled SQLite reads.** The current claim path is `SELECT … LIMIT 1` followed by an `UPDATE` — never written for multi-writer contention, can double-claim under concurrent workers. Two reasonable destinations:
   - **Postgres queue with `SELECT … FOR UPDATE SKIP LOCKED`** — no new infrastructure beyond what (1) already adds, fits the existing repository pattern.
   - **Managed Redis queue (BullMQ, Upstash QStash, or similar)** — leases, retries, and dead-letter handling out of the box; the right answer once queue depth or fan-out outgrows Postgres' contention budget.
   Either way add lease timeouts, max-retry caps, and a dead-letter table so a stuck worker can't park a job forever.

For `bulletproof_bt`, none of these changes are required — it's happy with a `ReadWriteOnce` PVC for the dashboard SQLite.

The deployment topology I'd recommend:

```
                                    Internet
                                       │
                                       ▼
                              ┌────────────────┐
                              │  Ingress (NGINX│
                              │  + cert-manager│
                              │   Let's Encrypt│
                              └───┬────────┬───┘
                                  │        │
                  ┌───────────────┘        └───────────────┐
                  ▼                                        ▼
          ┌──────────────┐                         ┌──────────────┐
          │ invariance-  │                         │ bulletproof- │
          │ web (Next)   │                         │ dashboard    │
          │ Deployment   │                         │ Deployment   │
          │ HPA 2..N     │                         │ replicas: 1  │
          └──────┬───────┘                         └──────┬───────┘
                 │                                        │
                 ▼                                        ▼
          ┌──────────────┐                         ┌──────────────┐
          │ Postgres     │                         │ PVC (sqlite) │
          │ (managed)    │                         └──────────────┘
          └──────┬───────┘
                 │
                 ├──── invariance-analysis-worker (Deployment, HPA 1..N)
                 │     image bundles bulletproof_bt + Python + Node bridge
                 │
                 └──── invariance-export-worker (Deployment, HPA 1..N)

          + S3 / R2 bucket for uploads, exports, benchmark library
          + Sealed Secrets / ExternalSecrets for STRIPE_*, BYBIT_*, NEXTAUTH_SECRET
          + Prometheus + Grafana for /api/health scraping
```

**Alternative split:** put `invariance-web` on Vercel and run only the workers + `bulletproof-dashboard` in your cluster. Vercel handles TLS, CDN, and edge for the Next.js tier with near-zero ops cost — but it **cannot** host the analysis or export workers (long-lived poll loops, Python subprocess, filesystem-bound bridge are all dealbreakers), so the worker tier still has to live somewhere container-shaped (k8s, Proxmox, ECS, Cloud Run jobs). Pick this only if you don't already need k8s for the bulletproof dashboard; otherwise the all-in-cluster topology above keeps the deploy surface smaller.

### 4.2 Image registry and CI

Pick one registry (GHCR, ECR, GAR, Docker Hub). Tag images as `<registry>/<project>:<git-sha>` and `:latest` is for laziness only. CI pipeline outline:

```
on push:
  - lint + test both repos
  - build images:
      bulletproof-bt-engine:<sha>
      bulletproof-dashboard:<sha>
      invariance-web:<sha>
      invariance-worker:<sha>          # bundles Python + bt + Node
  - docker push <registry>/<image>:<sha>
  - kubectl set image deployment/<name> <container>=<registry>/<image>:<sha>
    (or update Helm values + `helm upgrade`)
```

### 4.3 Dockerfiles

#### 4.3.1 `bulletproof_bt` — a base "engine" image and a dashboard image

The base image is what `invariance_research`'s analysis worker also imports from. Building it once and reusing is the cheapest route.

`bulletproof_bt/Dockerfile`:

```dockerfile
# Base engine image — multi-stage to keep runtime small
FROM python:3.11-slim AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential gcc git && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
COPY configs ./configs
COPY orchestrator ./orchestrator

RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgomp1 && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -r bt && useradd -r -g bt -m -d /home/bt bt

COPY --from=builder /install /usr/local
COPY --from=builder /build /app
WORKDIR /app
USER bt

# Default: run the dashboard. Override CMD for daemon, scripts, or library use.
ENV PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "orchestrator/run_dashboard.py", \
     "--db", "/data/research.sqlite", \
     "--host", "0.0.0.0", \
     "--port", "8765"]
```

This same image runs:

- the **dashboard** (default `CMD`),
- the **research daemon** (`CMD ["python","orchestrator/research_daemon.py","--db","/data/research.sqlite","--config","/etc/bt/daemon.yaml"]`),
- a **batch backtest job** (`CMD ["python","scripts/run_backtest.py", ...]`),
- and the **`bt` import** that the invariance worker uses.

For invariance, you can either reuse this image or build a slimmer "library only" tag using `pip install --no-deps git+https://...@v0.2.2`.

#### 4.3.2 `invariance_research` — Next.js web image and worker image

Two images, both built from the same repo. The web image is small (Node only). The worker image needs Node **and** Python + the engine, so build it `FROM` the engine image.

`invariance_research/Dockerfile.web`:

```dockerfile
FROM node:22-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm npm ci

FROM node:22-bookworm-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-bookworm-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN groupadd -r app && useradd -r -g app -m -d /home/app app
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
USER app
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
ENV INVARIANCE_EMBEDDED_WORKERS=false
CMD ["node", "server.js"]
```

(Add `output: "standalone"` to `next.config.ts` to get the standalone build above.)

`invariance_research/Dockerfile.worker`:

```dockerfile
# FROM the engine image so the Python `bt` package and bridge runtime are present
FROM <registry>/bulletproof-bt-engine:<sha> AS engine

FROM node:22-bookworm-slim AS worker
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Bring the Python env from the engine image
COPY --from=engine /usr/local /usr/local

WORKDIR /app
COPY package.json package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY . .
RUN npx tsc --noEmit && npm run build || true   # type-check; build optional

ENV INVARIANCE_PYTHON_BIN=python3 \
    INVARIANCE_BULLETPROOF_BRIDGE_SCRIPT=/app/scripts/run_bulletproof_engine.py \
    INVARIANCE_EMBEDDED_WORKERS=false \
    NODE_ENV=production

# Default to the analysis worker; the export worker uses CMD override
CMD ["npx", "tsx", "scripts/run-analysis-worker.ts"]
```

The export-worker pod just sets `command: ["npx","tsx","scripts/run-export-worker.ts"]` against the same image.

### 4.4 Kubernetes manifests

A single namespace per project keeps RBAC and quotas tidy:

```bash
kubectl create namespace omenka-prod
```

#### 4.4.1 Secrets and config

Use **Sealed Secrets** or **External Secrets Operator** in real life — never commit raw secrets. For illustration:

```yaml
apiVersion: v1
kind: Secret
metadata: { name: invariance-secrets, namespace: omenka-prod }
stringData:
  NEXTAUTH_SECRET: "REPLACE_ME"
  STRIPE_SECRET_KEY: "sk_live_..."
  STRIPE_WEBHOOK_SECRET: "whsec_..."
  STRIPE_PRICE_PROFESSIONAL: "price_..."
  STRIPE_PRICE_RESEARCH_LAB: "price_..."
  DATABASE_URL: "postgres://invariance:...@pg/invariance"
  S3_ACCESS_KEY: "..."
  S3_SECRET_KEY: "..."
---
apiVersion: v1
kind: ConfigMap
metadata: { name: invariance-config, namespace: omenka-prod }
data:
  INVARIANCE_EMBEDDED_WORKERS: "false"
  INVARIANCE_ANALYSIS_WORKER_POLL_MS: "1500"
  INVARIANCE_EXPORT_WORKER_POLL_MS: "1500"
  INVARIANCE_WORKER_STALE_MS: "60000"
  INVARIANCE_PYTHON_BIN: "python3"
  INVARIANCE_BULLETPROOF_BRIDGE_SCRIPT: "/app/scripts/run_bulletproof_engine.py"
  INVARIANCE_ENGINE_TIMEOUT_MS: "180000"
  S3_BUCKET: "invariance-prod"
  S3_ENDPOINT: "https://s3.eu-west-1.amazonaws.com"
  APP_URL: "https://invariance.example.com"
  ADMIN_EMAILS: "kaiwueke@gmail.com"
```

Bybit live keys for `bulletproof_bt` go in their own secret, mounted only on the live-exec pods.

#### 4.4.2 `invariance_research` web Deployment + Service

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: invariance-web, namespace: omenka-prod }
spec:
  replicas: 2
  selector: { matchLabels: { app: invariance-web } }
  template:
    metadata: { labels: { app: invariance-web } }
    spec:
      containers:
        - name: web
          image: <registry>/invariance-web:<sha>
          ports: [{ containerPort: 3000 }]
          envFrom:
            - configMapRef: { name: invariance-config }
            - secretRef: { name: invariance-secrets }
          readinessProbe:
            httpGet: { path: /api/health, port: 3000 }
            initialDelaySeconds: 10
          livenessProbe:
            httpGet: { path: /api/health, port: 3000 }
            initialDelaySeconds: 30
          resources:
            requests: { cpu: "200m", memory: "512Mi" }
            limits:   { cpu: "1000m", memory: "1Gi" }
---
apiVersion: v1
kind: Service
metadata: { name: invariance-web, namespace: omenka-prod }
spec:
  selector: { app: invariance-web }
  ports: [{ port: 80, targetPort: 3000 }]
```

#### 4.4.3 `invariance_research` analysis worker

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: invariance-analysis-worker, namespace: omenka-prod }
spec:
  replicas: 2
  selector: { matchLabels: { app: invariance-analysis-worker } }
  template:
    metadata: { labels: { app: invariance-analysis-worker } }
    spec:
      containers:
        - name: worker
          image: <registry>/invariance-worker:<sha>
          command: ["npx", "tsx", "scripts/run-analysis-worker.ts"]
          envFrom:
            - configMapRef: { name: invariance-config }
            - secretRef:    { name: invariance-secrets }
          resources:
            requests: { cpu: "500m", memory: "1Gi" }
            limits:   { cpu: "2",    memory: "4Gi" }   # engine is CPU-bound
```

A second deployment `invariance-export-worker` reuses the same image with `command: ["npx","tsx","scripts/run-export-worker.ts"]`.

Workers don't need a Service — they're pull-based. Use HPA on CPU and a custom metric like `analysis_jobs_pending` (export it from `/api/health` or a dedicated metrics endpoint):

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: { name: invariance-analysis-worker, namespace: omenka-prod }
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: invariance-analysis-worker
  minReplicas: 1
  maxReplicas: 10
  metrics:
    - type: Resource
      resource: { name: cpu, target: { type: Utilization, averageUtilization: 70 } }
```

#### 4.4.4 `bulletproof_bt` dashboard Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: bulletproof-dashboard, namespace: omenka-prod }
spec:
  replicas: 1                      # SQLite — single writer
  strategy: { type: Recreate }
  selector: { matchLabels: { app: bulletproof-dashboard } }
  template:
    metadata: { labels: { app: bulletproof-dashboard } }
    spec:
      containers:
        - name: dashboard
          image: <registry>/bulletproof-bt-engine:<sha>
          command: ["python", "orchestrator/run_dashboard.py",
                    "--db", "/data/research.sqlite",
                    "--host", "0.0.0.0", "--port", "8765"]
          ports: [{ containerPort: 8765 }]
          volumeMounts: [{ name: data, mountPath: /data }]
      volumes:
        - name: data
          persistentVolumeClaim: { claimName: bulletproof-data }
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: bulletproof-data, namespace: omenka-prod }
spec:
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 20Gi } }
---
apiVersion: v1
kind: Service
metadata: { name: bulletproof-dashboard, namespace: omenka-prod }
spec:
  selector: { app: bulletproof-dashboard }
  ports: [{ port: 80, targetPort: 8765 }]
```

A separate `bulletproof-research-daemon` Deployment with `replicas: 1` and the same image runs `research_daemon.py` against the same PVC.

### 4.5 Public exposure: Ingress + TLS

Install `ingress-nginx` and `cert-manager`, then create one `ClusterIssuer` for Let's Encrypt:

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: { name: letsencrypt-prod }
spec:
  acme:
    email: kaiwueke@gmail.com
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef: { name: letsencrypt-prod }
    solvers: [{ http01: { ingress: { class: nginx } } }]
```

Then a single Ingress per public host:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: omenka-ingress
  namespace: omenka-prod
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/proxy-body-size: "20m"   # Stripe webhooks + uploads
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - invariance.example.com
        - bt-dash.example.com
      secretName: omenka-tls
  rules:
    - host: invariance.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: { service: { name: invariance-web,        port: { number: 80 } } }
    - host: bt-dash.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: { service: { name: bulletproof-dashboard, port: { number: 80 } } }
```

DNS: point both A/AAAA records at the LoadBalancer IP of `ingress-nginx`. `cert-manager` will solve HTTP-01 and issue per-host certs into `omenka-tls`.

The `bulletproof-dashboard` host should not be public unless you want it to be. Either keep it internal-only (a separate `Ingress` with `nginx.ingress.kubernetes.io/whitelist-source-range`, or expose it only via VPN), or front it with OAuth2-Proxy + your IdP.

For the **Stripe webhook**, make sure `https://invariance.example.com/api/webhooks/stripe` is reachable from the public internet and that `STRIPE_WEBHOOK_SECRET` matches the secret Stripe shows you in the dashboard for that endpoint.

### 4.6 Database, storage, observability, security

- **Postgres:** prefer a managed service (Neon, Supabase, RDS, Cloud SQL). Provision once, store the DSN in `invariance-secrets.DATABASE_URL`, run migrations as a one-shot Job before each deploy.
- **Object storage:** S3 / R2 / GCS / MinIO. Bucket per environment. Workers and web both need read/write; use IRSA (EKS) / Workload Identity (GKE) / a static IAM user otherwise.
- **Observability:** Prometheus scrape `/api/health` (Next) and the dashboard. Grafana dashboards per app. `kubectl logs` is fine to start; ship to Loki or CloudWatch when you outgrow it.
- **HPA:** CPU is the easy metric. Real signal is queue depth — surface `analysis_jobs_pending` and `export_jobs_pending` as Prometheus counters and use a `custom.metrics.k8s.io` HPA on those.
- **NetworkPolicies:** default-deny in `omenka-prod`, then allow web → Postgres, workers → Postgres, workers → S3 endpoint, web → Stripe egress, web → S3 egress.
- **Pod security:** run as non-root (already done in the Dockerfiles), `readOnlyRootFilesystem: true`, drop all capabilities.
- **Backups:** Postgres PITR via the managed provider. S3 versioning + lifecycle. The bulletproof-dashboard PVC backed up via Velero or the cloud provider's snapshot API.
- **CI:** GitHub Actions or similar — `lint → test → build → push → kubectl set image`. Use a separate `staging` namespace mirroring `omenka-prod`.

### 4.7 Helm-chart-shaped layout (recommended)

Once the manifests above stop fitting in one folder, package each app as a Helm chart:

```
deploy/
├── charts/
│   ├── invariance/
│   │   ├── Chart.yaml
│   │   ├── values.yaml          # image tags, replica counts, env defaults
│   │   └── templates/
│   │       ├── deployment-web.yaml
│   │       ├── deployment-analysis-worker.yaml
│   │       ├── deployment-export-worker.yaml
│   │       ├── service.yaml
│   │       ├── hpa.yaml
│   │       ├── secret.yaml
│   │       └── configmap.yaml
│   └── bulletproof-bt/
│       ├── Chart.yaml
│       └── templates/
│           ├── deployment-dashboard.yaml
│           ├── deployment-daemon.yaml
│           ├── pvc.yaml
│           └── service.yaml
├── ingress/
│   └── omenka-ingress.yaml
└── cluster/
    ├── cert-manager-clusterissuer.yaml
    └── network-policies.yaml
```

Deploy with:

```bash
helm upgrade --install invariance     deploy/charts/invariance     -n omenka-prod -f values.prod.yaml
helm upgrade --install bulletproof-bt deploy/charts/bulletproof-bt -n omenka-prod -f values.prod.yaml
kubectl apply -f deploy/ingress/omenka-ingress.yaml
```

### 4.8 Order of operations on a fresh cluster

1. Create cluster, install `ingress-nginx` and `cert-manager`.
2. Provision managed Postgres + S3 bucket. Capture DSN + access keys.
3. Decide registry; push `bulletproof-bt-engine`, `invariance-web`, `invariance-worker` images.
4. `kubectl create namespace omenka-prod`.
5. Apply Sealed/External Secrets for Stripe, Bybit, NextAuth, DB, S3.
6. Apply ConfigMaps.
7. Apply each Deployment + Service + PVC + HPA.
8. Apply the `ClusterIssuer` and the `Ingress`.
9. Point DNS at the LoadBalancer.
10. Verify: hit `https://invariance.example.com/api/health` and the gated dashboard.
11. Configure Stripe webhook to `https://invariance.example.com/api/webhooks/stripe`.
12. Run a smoke analysis end-to-end (upload → analysis → export download).

That's the path from "two folders on a laptop" to "two services on the public internet, with TLS, autoscaling, and a real database."

### 4.9 Job queue hardening

The polled-DB queue is fine on a single replica but degrades quickly with multiple workers. To make it safe under horizontal scale:

- **Transactional leases.** Replace `SELECT … LIMIT 1` + `UPDATE` with `SELECT … FOR UPDATE SKIP LOCKED` (Postgres) or a managed-queue lease primitive. Eliminates the double-claim race.
- **Visibility timeout / lease expiry.** A worker that crashes mid-job must surrender its lease automatically. Bake `lease_expires_at` into the row (or use a queue that does it for you) and run a sweeper that re-queues expired leases.
- **Idempotent execution keys.** A retried job must not produce duplicate analyses or duplicate exports. Derive an idempotency key from `(analysis_id, attempt)` and short-circuit on conflict.
- **Max retries + dead-letter.** After N failures, move the job to a `dead_letter` table or queue and alert. Don't infinite-retry on poison payloads.
- **Decision: Redis vs Postgres queue.** If you're already running Postgres for app data, `SKIP LOCKED` is fewer moving parts. Switch to managed Redis only when queue depth or fan-out patterns outgrow Postgres' contention budget.

### 4.10 Auth and admin hardening

The current setup is `next-auth` credentials + scrypt + an env-allowlist for admins. To run in production:

- **Email verification on signup** — block login until the account confirms. Kills typo accounts and abuse signups.
- **Password reset flow** — signed token, short TTL, single-use.
- **Optional OAuth provider for operator/admin accounts** so a forgotten password doesn't lock out support.
- **DB-backed admin roles + audit log.** Replace `ADMIN_EMAILS` / `ADMIN_USER_IDS` with a `roles` table and an `admin_audit_log` table (who, when, target, before/after) on every admin mutation. Keep the env-allowlist as a bootstrap fallback only.
- **Session revocation.** JWT sessions today mean a compromised token is good until expiry. Add a `session_version` column on `users`, bump it on password change / explicit revoke, and reject stale tokens at validation.

### 4.11 Stripe production controls

Today: signature-verified webhook handler + `webhook_events` receipt table + `subscriptions` mirror. To launch:

- **Webhook replay tooling.** A minimal admin route + script to re-fire a stored `webhook_events` row through the handler — for the inevitable "we missed an event" or "the handler crashed mid-processing" case.
- **Failed-event alerting.** Anything that fails signature verification or handler processing should page someone (or land in a Slack/email alert), not just sit in the table.
- **Nightly reconciliation `CronJob`.** Walks active subscriptions, calls Stripe's API, and reports drift between local `subscriptions` rows and Stripe truth. Repair small drifts automatically; alert on big ones.
- **Strong account linkage on checkout.** Require `metadata.account_id` on every checkout session and reject events without it. Don't try to match by email after the fact.
- **Entitlement precedence rules.** Document explicitly which source wins when local state and Stripe disagree (recommended: Stripe is authoritative for billing status; local table is a cache).

### 4.12 Rate limiting and abuse controls

Upload + analysis are expensive — a single bad actor can chew through compute. Layer the controls:

- **Per-IP rate limits at ingress** for `/api/uploads/inspect`, `/api/analyses`, `/api/auth/*`, and `/api/billing/checkout`.
- **Per-account rate limits in middleware** — bind to `account_id` (not IP) using a small Redis token bucket. Different limits per plan tier.
- **Upload size + file-count caps** enforced before parsing — reject early in `/api/uploads/inspect`.
- **Concurrent-job caps per account** at job-claim time so one account can't starve the worker pool.
- **Per-account export rate limits** so exports can't be used as an indirect re-run of analyses.

---

## 5. Phased migration roadmap

Don't try to land Postgres + S3 + queue rewrite + containers + ingress in one PR. The order that minimises rework:

1. **Phase 0 — Freeze the contracts.** Lock the repository-layer DB interface, the `ObjectStorage` interface, and a queue interface (`enqueue / lease / ack / retry / dead-letter`). Write contract tests around the analysis + export lifecycle. Everything else is a swap behind these.
2. **Phase 1 — Postgres.** Port the SQL repositories, add transactional claim semantics (`SKIP LOCKED`), write the SQLite → Postgres data migration. Validate in staging with the existing local FS still in place.
3. **Phase 2 — Object storage.** Add the S3 adapter behind the existing interface. Migrate uploads, exports, and publication assets. Add signed-URL downloads.
4. **Phase 3 — Queue / worker hardening.** Disable embedded workers everywhere except dev. Move to a leased queue (Postgres `SKIP LOCKED` or managed Redis). Add dead-letter and retry caps.
5. **Phase 4 — Worker runtime packaging.** Build the combined Node + Python + `bt` worker image (§4.3.2). Add startup probes that exercise the bridge.
6. **Phase 5 — Auth + admin hardening (§4.10).** Email verification, password reset, DB-backed admin roles + audit log.
7. **Phase 6 — Stripe production readiness (§4.11).** Replay tooling, alerting, nightly reconciliation cron.
8. **Phase 7 — CI/CD.** GitHub Actions: lint → test → build → push → migration check → deploy. Separate dev/staging/prod env vars. Deploy gate behind successful migration + health.
9. **Phase 8 — Staging dress rehearsal.** Parity with prod, load-test the queue and upload paths, runbook + restore drill.
10. **Phase 9 — Post-launch.** Tune worker concurrency, cache the benchmark manifest, enforce rate limits.
11. **Phase 10 — Research Desk extensibility (§7).** Generalise the job model, add artifact lineage. Don't do this until 0–9 are solid.

Each phase ships independently. Each phase is reversible — the contract stays put while implementations swap underneath.

---

## 6. Scale plan for ~1,000 users

Likely bottlenecks, in order:

1. **SQLite write contention.** First thing that breaks under concurrent activity. Phase 1 fixes it.
2. **Single-host local storage.** Throughput + durability cap. Phase 2 fixes it.
3. **Worker claim race + polling ceiling.** Becomes acute as soon as you scale workers >1. Phase 3 fixes it.
4. **Python bridge process startup + CPU.** Each analysis spawns a subprocess; cold start + engine compute dominates per-job latency. Long-term mitigation: warm worker pool or persistent Python sidecar — but not until 1–3 are done.
5. **Embedded workers contending with web requests.** Set `INVARIANCE_EMBEDDED_WORKERS=false` everywhere outside dev.

What stays sync vs async:

- **Sync (request path):** auth, upload inspect (fast parsing only), checkout creation, listing, viewing.
- **Async (worker only):** running the engine, rendering exports, generating PDFs.
- **Webhook-triggered, durable:** Stripe events — accepted in the API but processed via a queued handler so a slow Stripe outage doesn't block users.

Caching + durability:

- Cache the benchmark manifest in memory (read-heavy, slow-changing).
- Analysis outputs and exports are immutable — store them in object storage and never overwrite.
- DB stays the source of truth for state transitions and audit.

What breaks first if nothing changes: a multi-replica deploy with shared SQLite + local FS — the locks and disk semantics don't survive horizontal scale. Second: duplicate-claimed jobs the moment a second worker is added.

---

## 7. Research Desk future-proofing

These changes are cheap if done now and expensive if retrofitted later:

- **Generic workflow envelope.** Today's `analysis_jobs` and `export_jobs` are bespoke tables. Define a typed workflow/step model now (state machine, resumable steps, typed inputs/outputs) so future agentic / multi-step tasks don't each invent their own queue table.
- **Artifact lineage graph.** Move from "upload → result blob" to a lineage model: input artifact → derived datasets → analysis outputs → publications/reports. A small schema addition now that pays off massively when agent workflows produce intermediate artifacts.
- **Language-agnostic worker contracts.** Define task contracts as JSON schema + storage keys, not Node-specific function signatures. Lets you add Python, Rust, or Go workers later without re-platforming.
- **Tenant-safe storage prefixes.** Bake `account_id` into every storage key now (`s3://bucket/<account_id>/uploads/...`). Trivial today, painful to retrofit once Research Desk multiplies artifact volume.
- **Adapter boundary around the Python seam.** Keep `bulletproof_bt` behind a single adapter so an additional engine or agent tool can plug in without touching the worker shell.

---

## 8. The first two weeks

A concrete starter plan that lines up with the phased roadmap above.

**Week 1**

1. Add Postgres-backed persistence behind the existing repository interfaces. Keep SQLite as a dev-only fallback.
2. Implement the S3-compatible object storage adapter. Migrate upload + export writes; leave publications for week 2 if it crowds the week.
3. Set `INVARIANCE_EMBEDDED_WORKERS=false` as the default outside `NODE_ENV=development`.
4. Containerise the analysis + export worker runtime. Bake Python + `bt` in. Add a startup probe that exercises the bridge.

**Week 2**

5. Replace polled DB claim with a leased queue (`SKIP LOCKED` on Postgres or managed Redis). Add idempotency keys and dead-letter handling.
6. Stripe: nightly reconciliation cron + webhook failure alerting + a manual replay tool.
7. CI pipeline: lint → tests → build → migration check → worker smoke test → push.
8. Stand up staging end-to-end: web tier (Vercel or k8s, per §4.1) + managed Postgres + managed Redis (if chosen) + object storage + one worker pod. Run an end-to-end synthetic upload → analysis → export.

By end of week 2 the data path should be production-shaped, even if traffic is still demo-scale.
# Omenka — Codebase Walkthrough & Deployment Guide

This document is a deep walkthrough of the two coupled projects living under `/home/kez/Documents/omenka/`:

- **`bulletproof_bt/`** — a deterministic, institutional-grade Python backtesting engine
- **`invariance_research/`** — a Next.js + TypeScript SaaS that wraps `bulletproof_bt` and sells strategy-validation reports

The two are tightly coupled — the SaaS literally imports the engine.

The final section covers Kubernetes packaging and public exposure for both.

---

## 1. The big picture

```
┌──────────────────────────────────────────────────────────────────┐
│                      invariance_research                         │
│  Next.js 15 (App Router) + TS + React 19 + Tailwind              │
│  Node backend (same process) + SQLite + Stripe + Auth.js         │
│  Spawns Python subprocess  ──────────────────────┐               │
└──────────────────────────────────────────────────┼───────────────┘
                                                   │ JSON over stdio
                                                   ▼
                                ┌──────────────────────────────────┐
                                │         bulletproof_bt           │
                                │  Python 3.11+ deterministic      │
                                │  backtesting engine              │
                                │  • CLI scripts                   │
                                │  • Public API: bt.run_*          │
                                │  • Optional FastAPI dashboard    │
                                │  • Optional research_daemon      │
                                │  • Live Bybit adapter (REST/WS)  │
                                └──────────────────────────────────┘
```

A typical request inside `invariance_research`:

1. User uploads a `.csv` or `.zip` of trade artifacts → `/api/uploads/inspect` parses, validates, and scores eligibility.
2. User confirms → `/api/analyses` POST inserts an `analysis_jobs` row in SQLite.
3. The analysis worker (embedded loop or split process) claims the row, **spawns `python3 scripts/run_bulletproof_engine.py`**, pipes the parsed artifact in as JSON, and waits.
4. The Python bridge calls `bt.run_analysis_from_parsed_artifact(...)` — this is the public API of the `bulletproof_bt` package (pinned to `v0.2.2` from GitHub).
5. The engine returns a JSON blob of figures (overview, distribution, monte-carlo, execution, regimes, ruin, stability, report). The Node side adapts those into product contracts and persists them.
6. The frontend renders ECharts visualisations over those persisted records; the user can also enqueue an export (JSON/Markdown/PDF) handled by the export worker.

Stripe handles billing and entitlements gate which diagnostics are available. Admin-only routes (allow-listed by env vars) cover ops dashboards: jobs, exports, webhooks, accounts, health, maintenance.

---

## 2. `bulletproof_bt` — the engine

### 2.1 What it is

A deterministic, event-driven, single-strategy quantitative backtester aimed at institutional research:

- Same data + same config → identical outputs (bit-for-bit reproducibility)
- Multi-asset: crypto (24/7), forex (24×5), equities (session-based), futures
- Every run produces a versioned, auditable artifact bundle
- Strict invariants between strategy ↔ risk ↔ execution ↔ portfolio ↔ feed ↔ benchmark ↔ artifacts
- **Out of scope (V1):** multi-strategy blending, portfolio allocation, tick/order-book simulation, web dashboards as production surfaces

### 2.2 Layout

```
bulletproof_bt/
├── src/bt/                   # ~315 Python files — the engine itself
├── orchestrator/             # FastAPI dashboard, research daemon, pipeline runner
├── scripts/                  # 20 CLI entry points
├── configs/                  # Layered YAML packs (engine, fees, slippage, exec, experiments)
├── tests/                    # ~170 deterministic / contract / regression tests
├── docs/                     # ~74 markdown contracts and runbooks
├── examples/reference_artifacts/  # Reference outputs used by regression tests
├── research/                 # Hypotheses and audit notes
├── data/                     # Sample datasets
└── debug/                    # Debug helpers
```

### 2.3 Core modules (`src/bt/`)

| Module | Responsibility |
|---|---|
| `core/` | Engine loop (`engine.py`), config resolution, clocks, reason codes |
| `data/` | Dataset loading, validation, resampling, streaming feeds, market rules |
| `execution/` | Execution profiles, pricing, slippage, spread, commission, fees |
| `risk/` | Position sizing, margin modeling, stop handling |
| `portfolio/` | Cash management, positions, liquidation, accounting |
| `metrics/` | Performance, attribution, reconciliation, R-metrics |
| `logging/` | Artifact writers, trade schemas, decision traces |
| `benchmarks/` | `buy_hold` / `flat` / `baseline` modes, comparison metrics |
| `instruments/` | Asset-class abstractions (forex/equity/crypto/futures specs) |
| `strategy/` | Strategy base classes, built-ins, context views |
| `indicators/` | 45+ streaming indicators (MA, RSI, ATR, Bollinger, Supertrend, …) |
| `orders/` | Order side validation/resolution |
| `universe/` | Universe filtering (history, volume, lag) |
| `analysis/` | Overview payload + feature extraction |
| `audit/` | Determinism + signal/order/fill/position/portfolio audits |
| `validation/` | Config completeness + schema versions |
| `experiments/` | Hypothesis & experiment configuration |
| `features/` | Online state layer, price-action features |
| `hypotheses/` | Hypothesis registry & base types |
| `contracts/` | Schema versions across all artifact types |
| `saas/` | `StrategyRobustnessLabService` — used by the SaaS layer |
| `exec/` | Live execution: Bybit adapter (REST + WS), simulated, paper, shadow |
| `benchmark/` | Legacy benchmark module |

Public API (`bt/__init__.py`):

```python
from bt import (
    run_backtest,
    run_grid,
    run_analysis_from_parsed_artifact,   # ← what invariance_research calls
    __version__,
)
```

`src/bt/api.py` (~22 KB) is the thin facade. The heavy lifting is `src/bt/core/engine.py` (~27 KB) and `src/bt/saas/service.py` (~177 KB — the SaaS feature surface).

### 2.4 Running it

| Surface | Entry | Purpose |
|---|---|---|
| Library | `from bt import run_backtest, run_grid, run_analysis_from_parsed_artifact` | Programmatic use |
| CLI | `scripts/run_backtest.py`, `run_experiment_grid.py`, `run_parallel_grid.py`, … (20 scripts) | Single runs, parameter sweeps, parallel grids, post-run analysis, MFE diagnostics, dataset extraction |
| Live exec CLI | `scripts/run_exec_bybit_demo.py`, `run_exec_bybit_live.py`, `run_exec_paper.py`, `run_exec_shadow.py`, `run_exec_doctor.py` | Connects to Bybit demo/live/paper/shadow modes |
| Dashboard | `python orchestrator/run_dashboard.py --db <path> [--host …] [--port 8765]` | Optional FastAPI + Jinja2 + SQLite UI for browsing runs |
| Daemon | `python orchestrator/research_daemon.py --db <path> --config daemon.yaml` | Polls SQLite queue and runs hypothesis/experiment jobs |
| Pipeline | `python orchestrator/run_experiment_pipeline.py --hypothesis <path> --name <n> --max-workers N` | End-to-end multi-worker pipeline |

### 2.5 Configuration

YAML, deeply layered — base → fees → slippage → exec → experiment overrides → local config. Notable packs in `configs/`:

- `engine.yaml`, `fees.yaml`, `slippage.yaml`, `exec.yaml`
- `packs/crypto_v1.yaml`, `packs/fx_trad_v1.yaml`
- `exec/bybit_demo.yaml`, `exec/bybit_live.yaml`, `exec/bybit_live_canary.yaml`, `exec/paper_simulated.yaml`, `exec/shadow_simulated.yaml`
- `experiments/h1_volfloor_donchian.yaml`, `experiments/h1_volfloor_emapullback.yaml`
- `examples/{safe_client,strict_research,fx_safe_client,equity_safe_client}.yaml`

### 2.6 Dependencies

Core: `numpy`, `pandas`, `pyarrow`, `matplotlib`, `pyyaml`. Optional dashboard: `fastapi`, `uvicorn`, `jinja2`. Dev: `pytest`, `ruff`, `mypy`. **No** ORM, no distributed framework, no message broker — orchestration is multiprocessing + a small SQLite queue.

### 2.7 Run artifacts

Each run drops a directory like:

```
run_xxx/
  config_used.yaml
  performance.json
  equity.csv
  trades.csv
  fills.jsonl
  decisions.jsonl
  performance_by_bucket.csv
  cost_breakdown.json
  summary.txt
  run_manifest.json
  run_status.json
  benchmark_*           # if benchmarks enabled
```

These are exactly the files the SaaS layer parses and renders.

### 2.8 What does **not** ship

No Dockerfile, no Kubernetes manifests, no docker-compose, no CI/CD config. Today it's a Python virtualenv + scripts, optionally fronted by a local FastAPI dashboard.

---

## 3. `invariance_research` — the SaaS

### 3.1 What it is

A **monolithic Next.js 15 (App Router) app** that delivers paid, async, execution-aware strategy diagnostics. A user uploads trade artifacts; the app validates eligibility, charges per-plan, runs `bulletproof_bt` via a Python subprocess, persists the result, renders interactive diagnostic pages, and lets them export the result as JSON, Markdown, or PDF.

### 3.2 Stack

- **Frontend:** Next.js 15.1.0 + React 19 + TypeScript 5.7 + Tailwind 3.4 + ECharts
- **Backend:** Same Next.js process — server components, route handlers, server actions
- **Auth:** `next-auth` v5 beta (credentials, scrypt-hashed passwords, JWT sessions)
- **DB:** SQLite via Node 22's built-in `node:sqlite` (`DatabaseSync`), file at `.data/invariance.sqlite`
- **Storage:** Local filesystem under `.data/storage` (no S3 yet)
- **Billing:** Stripe (checkout + portal + webhooks)
- **Job queue:** SQLite-backed, polled by an embedded loop **or** by split `tsx`-run worker processes
- **Engine bridge:** Spawns `python3 scripts/run_bulletproof_engine.py`, JSON over stdio
- **Python dep:** `bulletproof_bt @ git+https://github.com/Chinedum-iwueke/bulletproof_bt.git@v0.2.2`

### 3.3 Routes

**Public (marketing + content):** `/`, `/about/[slug]`, `/pricing`, `/methodology`, `/research/[slug]`, `/research-standards`, `/research-desk`, `/strategy-validation`, `/robustness-lab`, `/lab`, `/contact`, `/account`, `/docs/lab`, `/ui-kit`.

**Authenticated app:**

- `/app`, `/app/analyses`, `/app/new-analysis`, `/app/billing`, `/app/upgrade`, `/app/settings`
- `/app/analyses/[id]/{overview,distribution,monte-carlo,execution,regimes,ruin,stability,report}`

**Admin (env-allow-listed):** `/app/admin/{jobs,webhooks,exports,accounts,health,maintenance,publications,waitlist}`.

**Auth:** `/(auth)/login`, `/(auth)/signup`.

### 3.4 API endpoints (selected)

User:
`POST /api/uploads/inspect`, `GET|POST /api/analyses`, `GET|PUT /api/analyses/[id]`, `GET /api/analyses/[id]/status`, `POST /api/analyses/[id]/retry`, `POST /api/analyses/[id]/exports`, `GET /api/exports/[id]`, `GET /api/exports/[id]/download`, `GET /api/usage`, `POST /api/billing/checkout`, `POST /api/billing/portal`.

Public:
`POST /api/auth/[...nextauth]`, `POST /api/auth/register`, `POST /api/waitlist`, `GET /api/health`, `GET /api/benchmark-library/manifest`, `GET /api/benchmark-library/health`, `GET /api/publications/assets/[...assetPath]`.

Admin:
`POST /api/admin/jobs/[id]/retry`, `POST /api/admin/exports/[id]/retry`, `POST /api/admin/webhooks/[id]/reprocess`, `POST /api/admin/maintenance/[action]`, plus publications and waitlist CRUD.

Webhook:
`POST /api/webhooks/stripe` (signature-verified).

### 3.5 Backend layout (`src/lib/server/`)

- `persistence/` — SQLite connection + 6 schema migrations (core tables, exports, heartbeats, benchmarks, runtime config, publications, waitlist)
- `repositories/` — analysis, artifact, job, export, export-job, webhook-event, worker-heartbeat (no ORM, raw SQL)
- `auth/` — NextAuth credential provider + session helpers
- `accounts/` — account lifecycle
- `services/` — `analysis-service`, `analysis-job-runner`, `upload-intake-service`, `analysis-normalizer`, `analysis-view-service`
- `ingestion/` — CSV + ZIP parsers, Zod schemas, eligibility classifier, semantic validators
- `engine/` — `bulletproof-client` (spawns Python subprocess), `bulletproof-runner`, types
- `adapters/bulletproof/` — maps engine output → product contracts (`map-analysis`, `map-overview`, `map-monte-carlo`, `map-report`, `map-engine-analysis-record`)
- `exports/` — service + renderer (JSON/MD/PDF) + models
- `workers/` — `analysis-worker`, `export-worker`, generic `worker-runtime` loop with heartbeat
- `entitlements/` — plan matrix (explorer / professional / research_lab / advisory), policy, monthly usage
- `billing/` — Stripe client, checkout, portal, webhook handler, billing config
- `admin/` — guards + jobs/exports/webhooks/accounts/health/maintenance services
- `ops/` — health checks, logger, startup validation (probes Python + bridge + engine)
- `storage/` — local FS adapter
- `waitlist/` — lead capture

### 3.6 Workers and the Python bridge

Two run modes, switched by `INVARIANCE_EMBEDDED_WORKERS`:

- `true` (default) — workers live in the Next.js process. Fine for dev; fails to scale.
- `false` — workers run as separate `tsx`-launched processes:
  - `npm run worker:analysis` → `scripts/run-analysis-worker.ts`
  - `npm run worker:export` → `scripts/run-export-worker.ts`

Both modes share the SQLite file and storage root, so on a single host they Just Work; across hosts you have to migrate to a shared DB and shared object store (the docs call this out explicitly as a deployment blocker).

The engine bridge (`scripts/run_bulletproof_engine.py`, ~19 KB) is the only Python in this repo. It:

1. Reads JSON payload from stdin
2. Imports `bt`
3. Validates against the runtime model seam
4. Calls `bt.run_analysis_from_parsed_artifact(parsed_artifact, config?)`
5. Emits JSON result on stdout
6. Surfaces structured error codes on failure

Probed on startup via a `--probe` flag and again via `/api/health`.

### 3.7 npm scripts and key env vars

```jsonc
{
  "dev": "next dev",
  "build": "next build",
  "start": "next start",
  "lint": "next lint",
  "worker:analysis": "tsx scripts/run-analysis-worker.ts",
  "worker:export":   "tsx scripts/run-export-worker.ts",
  "usage:recalculate": "tsx scripts/recalculate-usage.ts"
}
```

Selected env vars:

```
INVARIANCE_DB_PATH                  default .data/invariance.sqlite
INVARIANCE_STORAGE_ROOT             default .data/storage
INVARIANCE_EMBEDDED_WORKERS         true|false
INVARIANCE_ANALYSIS_WORKER_POLL_MS  poll interval
INVARIANCE_EXPORT_WORKER_POLL_MS    poll interval
INVARIANCE_WORKER_STALE_MS          heartbeat freshness
INVARIANCE_PYTHON_BIN               python3
INVARIANCE_BULLETPROOF_BRIDGE_SCRIPT  script path override
INVARIANCE_ENGINE_TIMEOUT_MS        default 120000
INVARIANCE_BENCHMARK_LIBRARY_ROOT
STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET
STRIPE_PRICE_PROFESSIONAL / STRIPE_PRICE_RESEARCH_LAB
ADMIN_EMAILS / ADMIN_USER_IDS       allow-list
APP_URL                             checkout/portal return URL
```

### 3.8 Production-readiness gaps (from the repo's own docs)

`docs/repo-aware-architecture-and-deployment-report-2026-04-17.md` calls these out:

- **SQLite is a hard ceiling** — needs migration to managed Postgres (Neon/Supabase/RDS).
- **Local filesystem coupling** — needs S3/R2/Backblaze + an object-storage adapter.
- **No Dockerfile / k8s / IaC** in repo — must be added.
- Web tier could go to Vercel; workers must be containerised separately because Vercel can't run long-lived poll loops or invoke Python subprocesses reliably.

---

## 4. Deploying to Kubernetes

This section covers Docker packaging and a public, TLS-terminated Kubernetes deployment for both projects. Everything below is a blueprint — adapt names, registries, and resource sizes to your actual cluster.

### 4.1 Architectural decisions before you write any YAML

Before packaging anything, three changes to `invariance_research` are effectively required for a real cluster (the repo's own docs flag the first two; the third becomes critical the moment you run more than one worker):

1. **Migrate persistence from SQLite (`node:sqlite`) to managed Postgres.** SQLite holds the job queue, Stripe webhook log, analyses, exports, accounts, etc. The current code uses `DatabaseSync`; a single replica per database file is the hard limit. For a real cluster you need either:
   - Postgres + a thin repository-layer rewrite (preferred), or
   - A `ReadWriteOnce` PVC with **exactly one replica** of every component that opens the DB (web, analysis worker, export worker) — fragile, defeats HPA, only buys you time.
2. **Move uploaded artifacts and exports off local disk to S3-compatible object storage.** The current `storage/` adapter reads/writes `.data/storage/...`. Replace with an S3 adapter (R2, MinIO, AWS S3) or you'll need a `ReadWriteMany` PVC shared between web + workers, which most clusters don't offer cheaply.
3. **Harden the job queue beyond polled SQLite reads.** The current claim path is `SELECT … LIMIT 1` followed by an `UPDATE` — never written for multi-writer contention, can double-claim under concurrent workers. Two reasonable destinations:
   - **Postgres queue with `SELECT … FOR UPDATE SKIP LOCKED`** — no new infrastructure beyond what (1) already adds, fits the existing repository pattern.
   - **Managed Redis queue (BullMQ, Upstash QStash, or similar)** — leases, retries, and dead-letter handling out of the box; the right answer once queue depth or fan-out outgrows Postgres' contention budget.
   Either way add lease timeouts, max-retry caps, and a dead-letter table so a stuck worker can't park a job forever.

For `bulletproof_bt`, none of these changes are required — it's happy with a `ReadWriteOnce` PVC for the dashboard SQLite.

The deployment topology I'd recommend:

```
                                    Internet
                                       │
                                       ▼
                              ┌────────────────┐
                              │  Ingress (NGINX│
                              │  + cert-manager│
                              │   Let's Encrypt│
                              └───┬────────┬───┘
                                  │        │
                  ┌───────────────┘        └───────────────┐
                  ▼                                        ▼
          ┌──────────────┐                         ┌──────────────┐
          │ invariance-  │                         │ bulletproof- │
          │ web (Next)   │                         │ dashboard    │
          │ Deployment   │                         │ Deployment   │
          │ HPA 2..N     │                         │ replicas: 1  │
          └──────┬───────┘                         └──────┬───────┘
                 │                                        │
                 ▼                                        ▼
          ┌──────────────┐                         ┌──────────────┐
          │ Postgres     │                         │ PVC (sqlite) │
          │ (managed)    │                         └──────────────┘
          └──────┬───────┘
                 │
                 ├──── invariance-analysis-worker (Deployment, HPA 1..N)
                 │     image bundles bulletproof_bt + Python + Node bridge
                 │
                 └──── invariance-export-worker (Deployment, HPA 1..N)

          + S3 / R2 bucket for uploads, exports, benchmark library
          + Sealed Secrets / ExternalSecrets for STRIPE_*, BYBIT_*, NEXTAUTH_SECRET
          + Prometheus + Grafana for /api/health scraping
```

**Alternative split:** put `invariance-web` on Vercel and run only the workers + `bulletproof-dashboard` in your cluster. Vercel handles TLS, CDN, and edge for the Next.js tier with near-zero ops cost — but it **cannot** host the analysis or export workers (long-lived poll loops, Python subprocess, filesystem-bound bridge are all dealbreakers), so the worker tier still has to live somewhere container-shaped (k8s, Proxmox, ECS, Cloud Run jobs). Pick this only if you don't already need k8s for the bulletproof dashboard; otherwise the all-in-cluster topology above keeps the deploy surface smaller.

### 4.2 Image registry and CI

Pick one registry (GHCR, ECR, GAR, Docker Hub). Tag images as `<registry>/<project>:<git-sha>` and `:latest` is for laziness only. CI pipeline outline:

```
on push:
  - lint + test both repos
  - build images:
      bulletproof-bt-engine:<sha>
      bulletproof-dashboard:<sha>
      invariance-web:<sha>
      invariance-worker:<sha>          # bundles Python + bt + Node
  - docker push <registry>/<image>:<sha>
  - kubectl set image deployment/<name> <container>=<registry>/<image>:<sha>
    (or update Helm values + `helm upgrade`)
```

### 4.3 Dockerfiles

#### 4.3.1 `bulletproof_bt` — a base "engine" image and a dashboard image

The base image is what `invariance_research`'s analysis worker also imports from. Building it once and reusing is the cheapest route.

`bulletproof_bt/Dockerfile`:

```dockerfile
# Base engine image — multi-stage to keep runtime small
FROM python:3.11-slim AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential gcc git && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
COPY configs ./configs
COPY orchestrator ./orchestrator

RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgomp1 && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -r bt && useradd -r -g bt -m -d /home/bt bt

COPY --from=builder /install /usr/local
COPY --from=builder /build /app
WORKDIR /app
USER bt

# Default: run the dashboard. Override CMD for daemon, scripts, or library use.
ENV PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "orchestrator/run_dashboard.py", \
     "--db", "/data/research.sqlite", \
     "--host", "0.0.0.0", \
     "--port", "8765"]
```

This same image runs:

- the **dashboard** (default `CMD`),
- the **research daemon** (`CMD ["python","orchestrator/research_daemon.py","--db","/data/research.sqlite","--config","/etc/bt/daemon.yaml"]`),
- a **batch backtest job** (`CMD ["python","scripts/run_backtest.py", ...]`),
- and the **`bt` import** that the invariance worker uses.

For invariance, you can either reuse this image or build a slimmer "library only" tag using `pip install --no-deps git+https://...@v0.2.2`.

#### 4.3.2 `invariance_research` — Next.js web image and worker image

Two images, both built from the same repo. The web image is small (Node only). The worker image needs Node **and** Python + the engine, so build it `FROM` the engine image.

`invariance_research/Dockerfile.web`:

```dockerfile
FROM node:22-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm npm ci

FROM node:22-bookworm-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-bookworm-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN groupadd -r app && useradd -r -g app -m -d /home/app app
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
USER app
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
ENV INVARIANCE_EMBEDDED_WORKERS=false
CMD ["node", "server.js"]
```

(Add `output: "standalone"` to `next.config.ts` to get the standalone build above.)

`invariance_research/Dockerfile.worker`:

```dockerfile
# FROM the engine image so the Python `bt` package and bridge runtime are present
FROM <registry>/bulletproof-bt-engine:<sha> AS engine

FROM node:22-bookworm-slim AS worker
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Bring the Python env from the engine image
COPY --from=engine /usr/local /usr/local

WORKDIR /app
COPY package.json package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY . .
RUN npx tsc --noEmit && npm run build || true   # type-check; build optional

ENV INVARIANCE_PYTHON_BIN=python3 \
    INVARIANCE_BULLETPROOF_BRIDGE_SCRIPT=/app/scripts/run_bulletproof_engine.py \
    INVARIANCE_EMBEDDED_WORKERS=false \
    NODE_ENV=production

# Default to the analysis worker; the export worker uses CMD override
CMD ["npx", "tsx", "scripts/run-analysis-worker.ts"]
```

The export-worker pod just sets `command: ["npx","tsx","scripts/run-export-worker.ts"]` against the same image.

### 4.4 Kubernetes manifests

A single namespace per project keeps RBAC and quotas tidy:

```bash
kubectl create namespace omenka-prod
```

#### 4.4.1 Secrets and config

Use **Sealed Secrets** or **External Secrets Operator** in real life — never commit raw secrets. For illustration:

```yaml
apiVersion: v1
kind: Secret
metadata: { name: invariance-secrets, namespace: omenka-prod }
stringData:
  NEXTAUTH_SECRET: "REPLACE_ME"
  STRIPE_SECRET_KEY: "sk_live_..."
  STRIPE_WEBHOOK_SECRET: "whsec_..."
  STRIPE_PRICE_PROFESSIONAL: "price_..."
  STRIPE_PRICE_RESEARCH_LAB: "price_..."
  DATABASE_URL: "postgres://invariance:...@pg/invariance"
  S3_ACCESS_KEY: "..."
  S3_SECRET_KEY: "..."
---
apiVersion: v1
kind: ConfigMap
metadata: { name: invariance-config, namespace: omenka-prod }
data:
  INVARIANCE_EMBEDDED_WORKERS: "false"
  INVARIANCE_ANALYSIS_WORKER_POLL_MS: "1500"
  INVARIANCE_EXPORT_WORKER_POLL_MS: "1500"
  INVARIANCE_WORKER_STALE_MS: "60000"
  INVARIANCE_PYTHON_BIN: "python3"
  INVARIANCE_BULLETPROOF_BRIDGE_SCRIPT: "/app/scripts/run_bulletproof_engine.py"
  INVARIANCE_ENGINE_TIMEOUT_MS: "180000"
  S3_BUCKET: "invariance-prod"
  S3_ENDPOINT: "https://s3.eu-west-1.amazonaws.com"
  APP_URL: "https://invariance.example.com"
  ADMIN_EMAILS: "kaiwueke@gmail.com"
```

Bybit live keys for `bulletproof_bt` go in their own secret, mounted only on the live-exec pods.

#### 4.4.2 `invariance_research` web Deployment + Service

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: invariance-web, namespace: omenka-prod }
spec:
  replicas: 2
  selector: { matchLabels: { app: invariance-web } }
  template:
    metadata: { labels: { app: invariance-web } }
    spec:
      containers:
        - name: web
          image: <registry>/invariance-web:<sha>
          ports: [{ containerPort: 3000 }]
          envFrom:
            - configMapRef: { name: invariance-config }
            - secretRef: { name: invariance-secrets }
          readinessProbe:
            httpGet: { path: /api/health, port: 3000 }
            initialDelaySeconds: 10
          livenessProbe:
            httpGet: { path: /api/health, port: 3000 }
            initialDelaySeconds: 30
          resources:
            requests: { cpu: "200m", memory: "512Mi" }
            limits:   { cpu: "1000m", memory: "1Gi" }
---
apiVersion: v1
kind: Service
metadata: { name: invariance-web, namespace: omenka-prod }
spec:
  selector: { app: invariance-web }
  ports: [{ port: 80, targetPort: 3000 }]
```

#### 4.4.3 `invariance_research` analysis worker

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: invariance-analysis-worker, namespace: omenka-prod }
spec:
  replicas: 2
  selector: { matchLabels: { app: invariance-analysis-worker } }
  template:
    metadata: { labels: { app: invariance-analysis-worker } }
    spec:
      containers:
        - name: worker
          image: <registry>/invariance-worker:<sha>
          command: ["npx", "tsx", "scripts/run-analysis-worker.ts"]
          envFrom:
            - configMapRef: { name: invariance-config }
            - secretRef:    { name: invariance-secrets }
          resources:
            requests: { cpu: "500m", memory: "1Gi" }
            limits:   { cpu: "2",    memory: "4Gi" }   # engine is CPU-bound
```

A second deployment `invariance-export-worker` reuses the same image with `command: ["npx","tsx","scripts/run-export-worker.ts"]`.

Workers don't need a Service — they're pull-based. Use HPA on CPU and a custom metric like `analysis_jobs_pending` (export it from `/api/health` or a dedicated metrics endpoint):

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: { name: invariance-analysis-worker, namespace: omenka-prod }
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: invariance-analysis-worker
  minReplicas: 1
  maxReplicas: 10
  metrics:
    - type: Resource
      resource: { name: cpu, target: { type: Utilization, averageUtilization: 70 } }
```

#### 4.4.4 `bulletproof_bt` dashboard Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: bulletproof-dashboard, namespace: omenka-prod }
spec:
  replicas: 1                      # SQLite — single writer
  strategy: { type: Recreate }
  selector: { matchLabels: { app: bulletproof-dashboard } }
  template:
    metadata: { labels: { app: bulletproof-dashboard } }
    spec:
      containers:
        - name: dashboard
          image: <registry>/bulletproof-bt-engine:<sha>
          command: ["python", "orchestrator/run_dashboard.py",
                    "--db", "/data/research.sqlite",
                    "--host", "0.0.0.0", "--port", "8765"]
          ports: [{ containerPort: 8765 }]
          volumeMounts: [{ name: data, mountPath: /data }]
      volumes:
        - name: data
          persistentVolumeClaim: { claimName: bulletproof-data }
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: bulletproof-data, namespace: omenka-prod }
spec:
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 20Gi } }
---
apiVersion: v1
kind: Service
metadata: { name: bulletproof-dashboard, namespace: omenka-prod }
spec:
  selector: { app: bulletproof-dashboard }
  ports: [{ port: 80, targetPort: 8765 }]
```

A separate `bulletproof-research-daemon` Deployment with `replicas: 1` and the same image runs `research_daemon.py` against the same PVC.

### 4.5 Public exposure: Ingress + TLS

Install `ingress-nginx` and `cert-manager`, then create one `ClusterIssuer` for Let's Encrypt:

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: { name: letsencrypt-prod }
spec:
  acme:
    email: kaiwueke@gmail.com
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef: { name: letsencrypt-prod }
    solvers: [{ http01: { ingress: { class: nginx } } }]
```

Then a single Ingress per public host:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: omenka-ingress
  namespace: omenka-prod
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/proxy-body-size: "20m"   # Stripe webhooks + uploads
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - invariance.example.com
        - bt-dash.example.com
      secretName: omenka-tls
  rules:
    - host: invariance.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: { service: { name: invariance-web,        port: { number: 80 } } }
    - host: bt-dash.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: { service: { name: bulletproof-dashboard, port: { number: 80 } } }
```

DNS: point both A/AAAA records at the LoadBalancer IP of `ingress-nginx`. `cert-manager` will solve HTTP-01 and issue per-host certs into `omenka-tls`.

The `bulletproof-dashboard` host should not be public unless you want it to be. Either keep it internal-only (a separate `Ingress` with `nginx.ingress.kubernetes.io/whitelist-source-range`, or expose it only via VPN), or front it with OAuth2-Proxy + your IdP.

For the **Stripe webhook**, make sure `https://invariance.example.com/api/webhooks/stripe` is reachable from the public internet and that `STRIPE_WEBHOOK_SECRET` matches the secret Stripe shows you in the dashboard for that endpoint.

### 4.6 Database, storage, observability, security

- **Postgres:** prefer a managed service (Neon, Supabase, RDS, Cloud SQL). Provision once, store the DSN in `invariance-secrets.DATABASE_URL`, run migrations as a one-shot Job before each deploy.
- **Object storage:** S3 / R2 / GCS / MinIO. Bucket per environment. Workers and web both need read/write; use IRSA (EKS) / Workload Identity (GKE) / a static IAM user otherwise.
- **Observability:** Prometheus scrape `/api/health` (Next) and the dashboard. Grafana dashboards per app. `kubectl logs` is fine to start; ship to Loki or CloudWatch when you outgrow it.
- **HPA:** CPU is the easy metric. Real signal is queue depth — surface `analysis_jobs_pending` and `export_jobs_pending` as Prometheus counters and use a `custom.metrics.k8s.io` HPA on those.
- **NetworkPolicies:** default-deny in `omenka-prod`, then allow web → Postgres, workers → Postgres, workers → S3 endpoint, web → Stripe egress, web → S3 egress.
- **Pod security:** run as non-root (already done in the Dockerfiles), `readOnlyRootFilesystem: true`, drop all capabilities.
- **Backups:** Postgres PITR via the managed provider. S3 versioning + lifecycle. The bulletproof-dashboard PVC backed up via Velero or the cloud provider's snapshot API.
- **CI:** GitHub Actions or similar — `lint → test → build → push → kubectl set image`. Use a separate `staging` namespace mirroring `omenka-prod`.

### 4.7 Helm-chart-shaped layout (recommended)

Once the manifests above stop fitting in one folder, package each app as a Helm chart:

```
deploy/
├── charts/
│   ├── invariance/
│   │   ├── Chart.yaml
│   │   ├── values.yaml          # image tags, replica counts, env defaults
│   │   └── templates/
│   │       ├── deployment-web.yaml
│   │       ├── deployment-analysis-worker.yaml
│   │       ├── deployment-export-worker.yaml
│   │       ├── service.yaml
│   │       ├── hpa.yaml
│   │       ├── secret.yaml
│   │       └── configmap.yaml
│   └── bulletproof-bt/
│       ├── Chart.yaml
│       └── templates/
│           ├── deployment-dashboard.yaml
│           ├── deployment-daemon.yaml
│           ├── pvc.yaml
│           └── service.yaml
├── ingress/
│   └── omenka-ingress.yaml
└── cluster/
    ├── cert-manager-clusterissuer.yaml
    └── network-policies.yaml
```

Deploy with:

```bash
helm upgrade --install invariance     deploy/charts/invariance     -n omenka-prod -f values.prod.yaml
helm upgrade --install bulletproof-bt deploy/charts/bulletproof-bt -n omenka-prod -f values.prod.yaml
kubectl apply -f deploy/ingress/omenka-ingress.yaml
```

### 4.8 Order of operations on a fresh cluster

1. Create cluster, install `ingress-nginx` and `cert-manager`.
2. Provision managed Postgres + S3 bucket. Capture DSN + access keys.
3. Decide registry; push `bulletproof-bt-engine`, `invariance-web`, `invariance-worker` images.
4. `kubectl create namespace omenka-prod`.
5. Apply Sealed/External Secrets for Stripe, Bybit, NextAuth, DB, S3.
6. Apply ConfigMaps.
7. Apply each Deployment + Service + PVC + HPA.
8. Apply the `ClusterIssuer` and the `Ingress`.
9. Point DNS at the LoadBalancer.
10. Verify: hit `https://invariance.example.com/api/health` and the gated dashboard.
11. Configure Stripe webhook to `https://invariance.example.com/api/webhooks/stripe`.
12. Run a smoke analysis end-to-end (upload → analysis → export download).

That's the path from "two folders on a laptop" to "two services on the public internet, with TLS, autoscaling, and a real database."

### 4.9 Job queue hardening

The polled-DB queue is fine on a single replica but degrades quickly with multiple workers. To make it safe under horizontal scale:

- **Transactional leases.** Replace `SELECT … LIMIT 1` + `UPDATE` with `SELECT … FOR UPDATE SKIP LOCKED` (Postgres) or a managed-queue lease primitive. Eliminates the double-claim race.
- **Visibility timeout / lease expiry.** A worker that crashes mid-job must surrender its lease automatically. Bake `lease_expires_at` into the row (or use a queue that does it for you) and run a sweeper that re-queues expired leases.
- **Idempotent execution keys.** A retried job must not produce duplicate analyses or duplicate exports. Derive an idempotency key from `(analysis_id, attempt)` and short-circuit on conflict.
- **Max retries + dead-letter.** After N failures, move the job to a `dead_letter` table or queue and alert. Don't infinite-retry on poison payloads.
- **Decision: Redis vs Postgres queue.** If you're already running Postgres for app data, `SKIP LOCKED` is fewer moving parts. Switch to managed Redis only when queue depth or fan-out patterns outgrow Postgres' contention budget.

### 4.10 Auth and admin hardening

The current setup is `next-auth` credentials + scrypt + an env-allowlist for admins. To run in production:

- **Email verification on signup** — block login until the account confirms. Kills typo accounts and abuse signups.
- **Password reset flow** — signed token, short TTL, single-use.
- **Optional OAuth provider for operator/admin accounts** so a forgotten password doesn't lock out support.
- **DB-backed admin roles + audit log.** Replace `ADMIN_EMAILS` / `ADMIN_USER_IDS` with a `roles` table and an `admin_audit_log` table (who, when, target, before/after) on every admin mutation. Keep the env-allowlist as a bootstrap fallback only.
- **Session revocation.** JWT sessions today mean a compromised token is good until expiry. Add a `session_version` column on `users`, bump it on password change / explicit revoke, and reject stale tokens at validation.

### 4.11 Stripe production controls

Today: signature-verified webhook handler + `webhook_events` receipt table + `subscriptions` mirror. To launch:

- **Webhook replay tooling.** A minimal admin route + script to re-fire a stored `webhook_events` row through the handler — for the inevitable "we missed an event" or "the handler crashed mid-processing" case.
- **Failed-event alerting.** Anything that fails signature verification or handler processing should page someone (or land in a Slack/email alert), not just sit in the table.
- **Nightly reconciliation `CronJob`.** Walks active subscriptions, calls Stripe's API, and reports drift between local `subscriptions` rows and Stripe truth. Repair small drifts automatically; alert on big ones.
- **Strong account linkage on checkout.** Require `metadata.account_id` on every checkout session and reject events without it. Don't try to match by email after the fact.
- **Entitlement precedence rules.** Document explicitly which source wins when local state and Stripe disagree (recommended: Stripe is authoritative for billing status; local table is a cache).

### 4.12 Rate limiting and abuse controls

Upload + analysis are expensive — a single bad actor can chew through compute. Layer the controls:

- **Per-IP rate limits at ingress** for `/api/uploads/inspect`, `/api/analyses`, `/api/auth/*`, and `/api/billing/checkout`.
- **Per-account rate limits in middleware** — bind to `account_id` (not IP) using a small Redis token bucket. Different limits per plan tier.
- **Upload size + file-count caps** enforced before parsing — reject early in `/api/uploads/inspect`.
- **Concurrent-job caps per account** at job-claim time so one account can't starve the worker pool.
- **Per-account export rate limits** so exports can't be used as an indirect re-run of analyses.

---

## 5. Phased migration roadmap

Don't try to land Postgres + S3 + queue rewrite + containers + ingress in one PR. The order that minimises rework:

1. **Phase 0 — Freeze the contracts.** Lock the repository-layer DB interface, the `ObjectStorage` interface, and a queue interface (`enqueue / lease / ack / retry / dead-letter`). Write contract tests around the analysis + export lifecycle. Everything else is a swap behind these.
2. **Phase 1 — Postgres.** Port the SQL repositories, add transactional claim semantics (`SKIP LOCKED`), write the SQLite → Postgres data migration. Validate in staging with the existing local FS still in place.
3. **Phase 2 — Object storage.** Add the S3 adapter behind the existing interface. Migrate uploads, exports, and publication assets. Add signed-URL downloads.
4. **Phase 3 — Queue / worker hardening.** Disable embedded workers everywhere except dev. Move to a leased queue (Postgres `SKIP LOCKED` or managed Redis). Add dead-letter and retry caps.
5. **Phase 4 — Worker runtime packaging.** Build the combined Node + Python + `bt` worker image (§4.3.2). Add startup probes that exercise the bridge.
6. **Phase 5 — Auth + admin hardening (§4.10).** Email verification, password reset, DB-backed admin roles + audit log.
7. **Phase 6 — Stripe production readiness (§4.11).** Replay tooling, alerting, nightly reconciliation cron.
8. **Phase 7 — CI/CD.** GitHub Actions: lint → test → build → push → migration check → deploy. Separate dev/staging/prod env vars. Deploy gate behind successful migration + health.
9. **Phase 8 — Staging dress rehearsal.** Parity with prod, load-test the queue and upload paths, runbook + restore drill.
10. **Phase 9 — Post-launch.** Tune worker concurrency, cache the benchmark manifest, enforce rate limits.
11. **Phase 10 — Research Desk extensibility (§7).** Generalise the job model, add artifact lineage. Don't do this until 0–9 are solid.

Each phase ships independently. Each phase is reversible — the contract stays put while implementations swap underneath.

---

## 6. Scale plan for ~1,000 users

Likely bottlenecks, in order:

1. **SQLite write contention.** First thing that breaks under concurrent activity. Phase 1 fixes it.
2. **Single-host local storage.** Throughput + durability cap. Phase 2 fixes it.
3. **Worker claim race + polling ceiling.** Becomes acute as soon as you scale workers >1. Phase 3 fixes it.
4. **Python bridge process startup + CPU.** Each analysis spawns a subprocess; cold start + engine compute dominates per-job latency. Long-term mitigation: warm worker pool or persistent Python sidecar — but not until 1–3 are done.
5. **Embedded workers contending with web requests.** Set `INVARIANCE_EMBEDDED_WORKERS=false` everywhere outside dev.

What stays sync vs async:

- **Sync (request path):** auth, upload inspect (fast parsing only), checkout creation, listing, viewing.
- **Async (worker only):** running the engine, rendering exports, generating PDFs.
- **Webhook-triggered, durable:** Stripe events — accepted in the API but processed via a queued handler so a slow Stripe outage doesn't block users.

Caching + durability:

- Cache the benchmark manifest in memory (read-heavy, slow-changing).
- Analysis outputs and exports are immutable — store them in object storage and never overwrite.
- DB stays the source of truth for state transitions and audit.

What breaks first if nothing changes: a multi-replica deploy with shared SQLite + local FS — the locks and disk semantics don't survive horizontal scale. Second: duplicate-claimed jobs the moment a second worker is added.

---

## 7. Research Desk future-proofing

These changes are cheap if done now and expensive if retrofitted later:

- **Generic workflow envelope.** Today's `analysis_jobs` and `export_jobs` are bespoke tables. Define a typed workflow/step model now (state machine, resumable steps, typed inputs/outputs) so future agentic / multi-step tasks don't each invent their own queue table.
- **Artifact lineage graph.** Move from "upload → result blob" to a lineage model: input artifact → derived datasets → analysis outputs → publications/reports. A small schema addition now that pays off massively when agent workflows produce intermediate artifacts.
- **Language-agnostic worker contracts.** Define task contracts as JSON schema + storage keys, not Node-specific function signatures. Lets you add Python, Rust, or Go workers later without re-platforming.
- **Tenant-safe storage prefixes.** Bake `account_id` into every storage key now (`s3://bucket/<account_id>/uploads/...`). Trivial today, painful to retrofit once Research Desk multiplies artifact volume.
- **Adapter boundary around the Python seam.** Keep `bulletproof_bt` behind a single adapter so an additional engine or agent tool can plug in without touching the worker shell.

---

## 8. The first two weeks

A concrete starter plan that lines up with the phased roadmap above.

**Week 1**

1. Add Postgres-backed persistence behind the existing repository interfaces. Keep SQLite as a dev-only fallback.
2. Implement the S3-compatible object storage adapter. Migrate upload + export writes; leave publications for week 2 if it crowds the week.
3. Set `INVARIANCE_EMBEDDED_WORKERS=false` as the default outside `NODE_ENV=development`.
4. Containerise the analysis + export worker runtime. Bake Python + `bt` in. Add a startup probe that exercises the bridge.

**Week 2**

5. Replace polled DB claim with a leased queue (`SKIP LOCKED` on Postgres or managed Redis). Add idempotency keys and dead-letter handling.
6. Stripe: nightly reconciliation cron + webhook failure alerting + a manual replay tool.
7. CI pipeline: lint → tests → build → migration check → worker smoke test → push.
8. Stand up staging end-to-end: web tier (Vercel or k8s, per §4.1) + managed Postgres + managed Redis (if chosen) + object storage + one worker pod. Run an end-to-end synthetic upload → analysis → export.

By end of week 2 the data path should be production-shaped, even if traffic is still demo-scale.
