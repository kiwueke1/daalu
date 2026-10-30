# 42. Local development setup

How to run Daalu on your laptop. The goal is "git clone →
run → see the operator app at localhost:3000 within 10
minutes."

---

## Prerequisites

- Python 3.12+
- Node 20+
- Docker (or Podman)
- `uv` (Python package manager — `pip install uv` or
  `curl -LsSf https://astral.sh/uv/install.sh | sh`)

---

## First-time setup

```bash
git clone https://github.com/kiwueke1/daalu.git
cd daalu

# Python deps
uv sync

# Frontend deps
cd frontend && npm install && cd ..

# Local env
cp dev/.env.example .env
# Edit .env — at minimum set DATABASE_URL, REDIS_URL,
# and at least one LLM provider key.

# Start dependencies (Postgres, Redis, Loki, Grafana)
docker compose -f dev/docker-compose.yaml up -d

# Run migrations
daalu db upgrade
```

---

## Running the app

Four processes, four terminals:

```bash
# 1. API
daalu server --host 0.0.0.0 --port 8000

# 2. Worker
daalu worker --concurrency=2

# 3. Beat
daalu beat

# 4. Frontend
cd frontend && npm run dev
```

Hit `http://localhost:3000`. The Next.js dev server proxies
API calls to `http://localhost:8000`.

For seeding demo data:

```bash
daalu seed         # tenant + admin user
daalu seed-demo    # synthetic alerts, events, integrations
```

---

## Auth in dev

The default seeded admin is `admin@example.com` / `admin`.
Use that to log in.

---

## Hot reload

- Backend: `daalu server --reload` (uvicorn watches files).
- Frontend: `npm run dev` watches by default.
- Worker: `daalu worker --autoreload` (Celery's reload
  facility; not perfect — sometimes a manual restart is
  needed).

---

## Database management

```bash
# create new migration
alembic revision --autogenerate -m "describe-change"

# upgrade
daalu db upgrade

# downgrade by N
daalu db downgrade -1

# rebuild from scratch (dev only)
docker compose -f dev/docker-compose.yaml down -v
docker compose -f dev/docker-compose.yaml up -d postgres
daalu db upgrade
daalu seed
```

---

## Running tests

```bash
# All tests
uv run pytest

# Just unit tests (fast)
uv run pytest tests/unit

# Just integration tests
uv run pytest tests/integration

# With coverage
uv run pytest --cov=daalu_automation --cov-report=html
```

Pre-commit hooks (ruff, mypy, pytest fast) run automatically
on commit. Skip with `--no-verify` only if you have to.

---

## Common dev tasks

### Reset everything

```bash
docker compose -f dev/docker-compose.yaml down -v
rm -rf frontend/.next
docker compose -f dev/docker-compose.yaml up -d
daalu db upgrade
daalu seed
```

### Inspect a tenant's data

```bash
daalu shell        # IPython with db session
>>> from daalu_automation.models import Tenant, User
>>> tenants = await db.execute(select(Tenant))
```

### Build the production image locally

```bash
docker build -t daalu-api:dev .
docker run -p 8000:8000 --env-file .env daalu-api:dev
```

### Hit a federated cluster from local

You can simulate federation with a local k3d cluster:

```bash
k3d cluster create test
# install daalu-edge with the dev hub URL pointing at your
# laptop's daalu-api via ngrok or similar
```

The dev hub mode uses a no-op WireGuard implementation.

---

## What's next

Chapter 43 — building images and the CI build pipeline.
