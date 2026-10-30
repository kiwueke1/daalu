# Getting started

This walks you through running the full stack locally. End state:
backend API at `:8000`, frontend at `:3000`, Celery worker + beat +
agent host all running, with synthetic events flowing so the UI is
populated.

## Prerequisites

- Python 3.11+
- Node.js 20+
- Docker (or local Postgres 16 + Redis 7)
- An Anthropic API key — strongly recommended; the platform works
  without one (heuristic fallbacks kick in) but the AI surface is
  dramatically better with it.

## 1. Backend

```bash
cd daalu-automation
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env` and set at minimum:

```
ANTHROPIC_API_KEY=sk-ant-...
SECRET_KEY=$(openssl rand -hex 32)
```

The other defaults work out of the box.

## 2. Infrastructure

```bash
docker run -d --name daalu-postgres \
  -e POSTGRES_DB=daalu_automation \
  -e POSTGRES_USER=daalu \
  -e POSTGRES_PASSWORD=daalu_password \
  -p 5432:5432 \
  postgres:16-alpine

docker run -d --name daalu-redis -p 6379:6379 redis:7-alpine
```

If you already run Postgres/Redis containers for another project, you
can share them — just point `DATABASE_URL` / `REDIS_URL` at
different databases / Redis DB numbers.

## 3. Seed the database

```bash
daalu seed         # creates the default tenant
daalu seed-demo    # generates a wave of synthetic infra events
```

You can run `seed-demo` repeatedly to keep populating the feed.

## 4. Run the services

Four terminals:

```bash
# Terminal 1 — API
daalu server --reload

# Terminal 2 — Celery worker
daalu worker

# Terminal 3 — Celery beat (scheduled briefings + ingest)
daalu beat

# Terminal 4 — Agent host (listens to the event stream)
daalu agents
```

API docs: `http://localhost:8000/docs`

## 5. Frontend

```bash
cd frontend
cp .env.local.example .env.local
npm install --legacy-peer-deps
npm run dev
```

Open `http://localhost:3000`. You should see:

- Hero briefing card (regenerate it with the button — `daalu briefing
  infra` from the CLI also works)
- Metric chips
- AI insight + recommendation cards
- Live event feed (events stream in over SSE)
- Workflow status card
- Sidebar with Operations, Agents, Automations, Reports, Alerts,
  Integrations
- AI command bar (⌘K) in the top header

## 6. Generate a briefing on demand

From the UI: click "Regenerate" on the hero briefing card.

From the CLI:

```bash
daalu briefing infra
```

From the API:

```bash
curl -X POST http://localhost:8000/api/v1/briefings/infra/generate
```

## 7. Trigger a real workflow

```bash
# Trigger the incident-coordinate workflow against a known incident
curl -X POST http://localhost:8000/api/v1/workflows/run \
  -H 'Content-Type: application/json' \
  -d '{"name":"infra.incident.coordinate","input":{"incident_id":"<id>"}}'

# Watch the run on the Automations page or:
curl 'http://localhost:8000/api/v1/workflows/runs?module=infra' | jq
```

## 8. Wire in a real integration

Once the platform is happy, point it at a real system — see the
[integrations catalog](book-customer/05-integrations-catalog/) in the
user handbook.

## Troubleshooting

- **"No LLM credentials configured"** — set `ANTHROPIC_API_KEY` or
  `LLM_API_KEY` and restart. Without it the platform runs but uses
  deterministic fallbacks.
- **SSE stream disconnects in dev** — Next dev proxy can be touchy.
  Refresh the page; the `EventSource` reconnects automatically.
- **Briefing fails with JSON parse error** — the LLM produced non-JSON.
  The error is recorded on the briefing row (`error_message`). Tune
  the system prompt in `src/daalu_automation/modules/<m>/prompts.py`.
