# System walkthrough — low-level design

This is the **how it actually runs** companion to
[`architecture.md`](architecture.md). Every section here points at the
exact files + line ranges that implement what's being described, so you
can read this with the codebase open and trace each subsystem end-to-end.

Convention: file paths are repo-relative; `path/to/file.py:L42-L80`
means lines 42–80 of that file.

---

## 1 · Process topology

The platform runs as **five long-lived processes** sharing one container
image, plus Postgres + Redis as state.

| Process | Command | Source | Replicas |
|---|---|---|---|
| API | `daalu server` | `src/daalu_automation/cli.py:32-37` → `src/daalu_automation/api/main.py:48-93` | 2–3 |
| Worker (Celery) | `daalu worker` | `src/daalu_automation/cli.py:40-46` → `src/daalu_automation/workers/celery_app.py:14-25` | 2–4 |
| Scheduler (Celery beat) | `daalu beat` | `src/daalu_automation/cli.py:49-54` → `src/daalu_automation/workers/celery_app.py:43-79` | 1 |
| Agent host | `daalu agents` | `src/daalu_automation/cli.py:57-60` → `src/daalu_automation/workers/agent_runner.py` | 2 |
| Frontend (Next.js) | `node server.js` | `frontend/` | 2 |

All four Python processes import `daalu_automation.modules`
(`src/daalu_automation/modules/__init__.py`), which triggers a
side-effect import of `infra/`, which in turn registers every agent,
briefing, integration, and workflow. By the time the process is
serving requests, every registry is fully populated — no manual
`app.register(...)` glue is needed.

```
                   ┌─────────────────────────────────────┐
                   │  External systems                   │
                   │  Prometheus / Loki / Nautobot       │
                   │  PagerDuty / Slack / SMTP / …       │
                   └────────────┬──────────┬─────────────┘
                                │ pull     │ push (webhooks)
                                ▼          ▼
   ┌─────────┐    REST / SSE   ┌──────────────────┐
   │ Next.js │ ◀───────────────▶│  FastAPI (API)   │
   │ :3000   │                  │  :8000           │
   └────┬────┘                  └─────┬────────────┘
        │                             │ publish events
        ▼                             ▼
   ┌─────────────────────────────────────────────────┐
   │  Redis  (stream: daalu.events)                  │
   │  Redis  (celery broker + result backend)        │
   └─┬─────────────────────────────┬─────────────────┘
     │ xreadgroup                  │ task delivery
     ▼                             ▼
   ┌────────────┐         ┌──────────────────┐    cron
   │ Agent host │         │ Celery worker    │ ◀────── ┌────────┐
   │ subscribe  │         │  briefings       │         │ beat   │
   │  → handle  │         │  ingest          │         │ tick   │
   │  → emit    │         │  stale-leads     │         └────────┘
   └─────┬──────┘         └────────┬─────────┘
         │ write derived rows      │
         ▼                         ▼
   ┌─────────────────────────────────────────────────┐
   │   Postgres                                      │
   │   events, alerts, recommendations, briefings,   │
   │   leads, opportunities, incidents, services …   │
   └─────────────────────────────────────────────────┘
```

The only stateful component is Postgres. Redis is recoverable — losing
it costs you in-flight events. The Python processes are stateless and
scale horizontally via Redis consumer-group semantics.

---

## 2 · Configuration & secrets

`src/daalu_automation/config.py:25-127` defines a Pydantic
`Settings` model. Every field has a default so the app boots even
without an `.env` present. `get_settings()` is `@lru_cache`'d so the
parsed object is shared across the process.

Key knobs:

| Setting | Default | What it controls |
|---|---|---|
| `anthropic_api_key` | `""` | Enables the LLM "quality" tier (`core/llm.py`) |
| `llm_api_key` + `llm_base_url` | `""` / DeepSeek URL | Optional OpenAI-compatible classifier tier |
| `redis_url` | `localhost:6379/0` | Event stream + Celery broker |
| `event_stream_key` | `daalu.events` | Redis stream key consumed by agents |
| `event_stream_group` | `daalu-workers` | Consumer group name |
| `daily_briefing_cron` | `0 7 * * 1-5` | Beat schedule for briefings |
| `slack_webhook_url` | `""` | Briefing + critical-alert delivery |
| `hubspot_api_key`, `prometheus_url`, `pagerduty_api_token` | `""` | Integrations that conditionally self-register |

`DEFAULT_TENANT_ID` (`config.py:21`) is the fixed UUID that pre-auth
rows are stamped with. Phase-2 multi-tenant rollout will turn this
into a per-request value derived from the auth token — until then,
every row in every table belongs to this single tenant.

---

## 3 · The event bus

The event bus is the **only** synchronisation point between modules.
Source: `src/daalu_automation/core/events.py`.

### 3.1 Envelope shape

`EventEnvelope` (`events.py:24-58`) is the on-the-wire record. The
contract is `module.noun.verb` for the `type` field:

```python
@dataclass(slots=True)
class EventEnvelope:
    type: str          # "infra.alert.fired"
    module: str        # "infra"
    source: str        # "prometheus" / "synthetic-infra" / "workflow"
    summary: str       # one-line for the UI feed
    payload: dict      # module-specific
    severity: str = "info"
    tenant_id: str = DEFAULT_TENANT_ID
    occurred_at: str = utcnow.isoformat()
    event_id: str = uuid4()
```

### 3.2 Publish path

`publish()` (`events.py:74-92`) does two things in one call:

1. `XADD` to the Redis stream `daalu.events` — instant fan-out to
   every agent consumer.
2. `_persist_event()` (`events.py:95-115`) writes an `Event` row to
   Postgres so the UI's `/events` REST list endpoint can query history
   without touching Redis.

Persist is opt-out (`persist=False`) for very high-volume tap events,
but every Phase-1 caller persists.

### 3.3 Subscribe path

`subscribe()` (`events.py:117-160`) is an async iterator. It joins the
shared consumer group (`daalu-workers`) under a per-process consumer
name and uses `XREADGROUP` with a 5-second block. Each yielded
`(stream_id, event)` is auto-acked after the handler returns
successfully — see `events.py:155-159`.

This is what makes horizontal scaling free: spinning up a second
`daalu-agents` pod adds another consumer to the same group; Redis
load-balances reads across them automatically.

### 3.4 SSE bridge to the frontend

The frontend doesn't consume the stream directly. Instead the API
exposes `/api/v1/events/stream`
(`src/daalu_automation/api/routers/events.py:71-122`) which uses
plain `XREAD` (no group) to tail the stream and re-emit each frame as
an SSE `operational-event`. The frontend's
`useEventStream` hook (`frontend/lib/sse.ts`) subscribes via
`EventSource` and animates entries into the live feed card
(`frontend/components/cards/event-feed-card.tsx`).

Heartbeats (`events.py:111`) prevent Cloudflare / nginx from killing
idle SSE connections — that's why the `daalu-frontend-ingress`
annotations turn off proxy buffering and bump the read timeout
(`deploy/k8s/frontend/ingress.yaml:9-12`).

---

## 4 · LLM client

`src/daalu_automation/core/llm.py` is the only place modules touch the
LLM. The signature is intentionally narrow:

```python
async def complete(*, system: str, user: str,
                   model: str | None = None,
                   tier: str = "quality",
                   max_tokens: int = 1024,
                   response_format: str = "text") -> LLMResult
```

### 4.1 Tier routing

`complete()` (`llm.py:39-72`) picks a provider based on `tier`:

- `tier="quality"` → Anthropic (`_anthropic()`, `llm.py:75-104`),
  which uses the same key Muse uses (`ANTHROPIC_API_KEY`).
- `tier="classifier"` → first checks `LLM_API_KEY` (OpenAI-compatible
  fallback — DeepSeek by default), only falls back to Anthropic if
  unset.

Modules opt in to the classifier tier when the call is cheap and
high-volume — e.g. alert triage classification before a heavier
quality-tier call.

### 4.2 Prompt caching

When `ANTHROPIC_CACHE_ENABLED=true` (default), the system prompt block
gets `{"cache_control": {"type": "ephemeral"}}` attached
(`llm.py:88-92`). The first briefing of the day pays full freight for
the ~1500-token system prompt; every subsequent briefing within the
TTL gets it free.

### 4.3 JSON helper

`complete_json()` (`llm.py:140-157`) wraps `complete()` for
prompts that need a structured response. It strict-parses the LLM
output and, on `JSONDecodeError`, extracts the first `{…}` block — a
defence against models that occasionally wrap JSON in code fences.

### 4.4 Graceful degradation

`LLMUnavailable` (`llm.py:34-37`) is raised when no credentials are
configured. Every caller in the platform catches it and falls back to
a deterministic path:

- `SalesAgent._score_lead` → heuristic scoring (`agent.py:151-163`)
- `InfraAgent._triage_alert` → "configure ANTHROPIC_API_KEY" placeholder
  triage (`infra/agent.py:50-56`)
- Briefing generators → `_offline_fallback()` renders a raw bullet list
  of events (`core/briefings.py:163-172`)

This is why `daalu seed-demo` produces meaningful UI even with an empty
`.env`.

---

## 5 · Agents

Source: `src/daalu_automation/core/agents.py`.

### 5.1 Base class

`Agent` (`agents.py:24-99`) is abstract. Subclasses set a
`descriptor: AgentDescriptor` class attribute and implement
`async def handle(self, event)`. The base class supplies:

- `should_handle(event)` — type filter (`agents.py:42-46`)
- `run_forever()` — the consumer-group loop (`agents.py:48-58`)
- `_record_run_start` / `_record_run_end` — persist an `AgentRun` row
  per invocation so the Agents page can show success-rate / last-seen
  (`agents.py:60-99`).

### 5.2 Registry

`register_agent()` (`agents.py:106-114`) is the decorator modules call
at import time. The registry is `_AGENT_FACTORIES` (`agents.py:103`).
`list_agents()` walks the registry — used by the
`/api/v1/agents` endpoint (`api/routers/agents.py:13-20`) and by
the agent host's `main()` (`workers/agent_runner.py:22-26`).

### 5.3 Helper writers

Agents almost always want to write derived state. `emit_alert()`
(`agents.py:125-153`) and `emit_recommendation()` (`agents.py:156-178`)
hide the SQLAlchemy details so the agent code stays readable.

### 5.4 InfraAgent — annotated walk-through

`src/daalu_automation/modules/infra/agent.py`.

```
handle(event):
  "infra.alert.fired" or
  "infra.deployment.failed"  → _triage_alert(event)         (L37-L101)
  "infra.alert.resolved"     → _auto_resolve_alert(event)   (L103-L106)
  "infra.capacity.warning"   → _recommend_capacity_action   (L108-L120)

_triage_alert:
  1. complete_json(system=INCIDENT_TRIAGE_SYSTEM, user=alert json,
                   tier="quality")  →  {root_cause, remediation,
                                         blast_radius, confidence}
  2. blast_radius → severity_map → (AlertSeverity, IncidentSeverity)
  3. Persist Incident row (Phase-1 storage)            (L66-L82)
  4. emit_alert(...) carrying the AI body + confidence (L84-L94)
  5. if Sev-1: emit_recommendation("Page on-call manager", 0.9)
```

`INCIDENT_TRIAGE_SYSTEM` (`modules/infra/prompts.py:3-18`) is
deliberately conservative: "never recommend destructive actions",
"escalate only when blast_radius is high" — because the
recommendations directly drive the cards a human acts on.

---

## 6 · Briefings

Source: `src/daalu_automation/core/briefings.py`.

### 6.1 Lifecycle

`BriefingGenerator.generate()` (`briefings.py:55-86`) is the only
public entry. It walks four phases:

```
_begin              → upsert Briefing row, status=generating       (L88-L113)
_load_context       → SELECT events WHERE occurred_at >= cutoff    (L115-L139)
                       AND relevant_event_filter()
_call_llm           → complete_json(system, user, max_tokens=2048) (L141-L153)
                       falls back to _offline_fallback on LLMUnavailable
derive_metrics      → extract metric chips from AI output          (L72-L73)
                       (modules can backfill from event counts)
commit              → status=ready, persist body + metrics +
                       source_event_ids                            (L74-L82)
```

Idempotency is enforced by `_begin()`: if a row already exists for
`(tenant_id, channel, coverage_date)`, it's resurrected with
`status=generating` rather than duplicated. This means the beat
scheduler can retry safely.

### 6.2 Infra briefing

`modules/infra/briefing.py:13-51` subclasses `BriefingGenerator`:

- `channel = BriefingChannel.infra`
- `system_prompt` returns the strict-JSON-emitting `BRIEFING_SYSTEM`
  from `modules/infra/prompts.py:21-49`
- `user_prompt` serialises the 24h event window as a JSON array — the
  LLM sees `infra.alert.fired`, `infra.deployment.failed`,
  `infra.capacity.warning` events
- `derive_metrics` trusts the LLM's metric block but backfills missing
  keys from event counts so the UI chip never shows "—"

### 6.3 Scheduling

`workers/celery_app.py:43-79` schedules the briefing on a cron:

```python
celery_app.conf.beat_schedule = {
    "infra-briefing": {"task": "infra.generate_briefing",
                       "schedule": _parse_cron(settings.daily_briefing_cron)},
    ...
}
```

The Celery task is a sync wrapper around the async generator —
`modules/infra/tasks.py:11-14`:

```python
@celery_app.task(name="infra.generate_briefing")
def generate_briefing_task() -> str:
    briefing = asyncio.run(InfraBriefingGenerator().generate())
    return str(briefing.id)
```

The API also exposes ad-hoc generation —
`/api/v1/briefings/{channel}/generate` runs it inline
(`api/routers/briefings.py:60-72`) so the UI's "Regenerate" button
returns the new row immediately rather than via job-poll.

---

## 7 · Integrations

Source: `src/daalu_automation/core/integrations.py`.

### 7.1 Adapter contract

`IntegrationAdapter` (`integrations.py:24-37`) has three methods:
`ingest()` (required, returns the number of events emitted), `push()`
(optional, for the action layer), `health()` (used by the
Integrations page).

### 7.2 Conditional registration

Real integrations only call `register_integration(...)` when their
credentials are present:

```python
# modules/infra/integrations.py
if get_settings().pagerduty_api_token:
    register_integration(PagerDutyAdapter)
```

This means `/api/v1/integrations` returns an accurate list of what's
wired up — the Integrations page in the UI can show a "needs setup"
chip for required-but-blank settings
(`api/routers/integrations.py:13-30`).

Synthetic adapters (`SyntheticInfraAdapter`) register unconditionally
so demos work with zero external creds.

### 7.3 Ingest flow

For the `SyntheticInfraAdapter` (`modules/infra/integrations.py`):

```
ingest():
  for each of 3–5 random synthetic alerts:
    1. INSERT Service row if absent
    2. publish(EventEnvelope(type="infra.alert.fired", ...))
       → InfraAgent picks it up via subscribe()
       → triages it, possibly raises a Recommendation
```

Each event generates an AI call plus the relevant DB writes. All of
it traceable back to a single integration trigger via the `source`
field on the Event row.

### 7.4 PagerDuty / Prometheus

`modules/infra/integrations.py:188-242` shows the "real adapter"
pattern: an `httpx.AsyncClient` call into the upstream API, response
parsing into envelope shape, `publish()` per row. These adapters are
intentionally thin — page state tracking, watermarks, dedup logic
belong in the upstream system or in Phase-2 enhancements to the
`Integration` row's `config` JSON field.

---

## 8 · Workflows

Source: `src/daalu_automation/core/workflows.py`.

### 8.1 Registration

`@register_workflow(name, module=…)` (`workflows.py:21-29`) wires an
`async def` into the registry. Today the runner is in-process
(`workflows.py:38-69`), but every workflow gets a `WorkflowRun` row
with started_at / finished_at / input / output / error_message — so
the Automations page in the UI shows history immediately.

### 8.2 Infra incident-coordination workflow

`modules/infra/workflows.py:14-58`:

```
@register_workflow("infra.incident.coordinate", module="infra")
async def coordinate_incident(payload):
    1. db.get(Incident) or create new from payload.summary
    2. set status=investigating
    3. _draft_status_update(title, root_cause, remediation)  →  LLM
    4. send_slack to #incidents
    5. publish "infra.incident.update"
```

### 8.3 Approve → dispatch

The Approve button on a recommendation card is the canonical entry
point. `api/routers/recommendations.py:32-55` does:

```
POST /api/v1/recommendations/{id}/approve
  → status=approved
```

This is the **closed loop** at the centre of the product — an AI
emits a recommendation, a human clicks approve, the workflow runs,
events fire, tomorrow's briefing reflects the action.

---

## 9 · API surface

`src/daalu_automation/api/main.py:48-93` mounts every router under
`/api/v1`. Routers are stateless and dependency-injected — the only
shared deps are the async DB session (`api/deps.py` →
`database.py::get_db`) and the tenant resolver (`api/deps.py:11-22`,
which today returns `DEFAULT_TENANT_ID` and is the Phase-2 hook for
auth).

### 9.1 Endpoint catalogue

| Method | Path | Source | Notes |
|---|---|---|---|
| GET | `/health` | `api/main.py:88-89` | k8s liveness/readiness |
| GET | `/api/v1/events` | `routers/events.py:21-44` | filterable list |
| POST | `/api/v1/events` | `routers/events.py:46-67` | generic ingestion webhook |
| GET | `/api/v1/events/stream` | `routers/events.py:71-122` | SSE feed |
| GET | `/api/v1/briefings` | `routers/briefings.py:25-44` | list with channel filter |
| GET | `/api/v1/briefings/latest?channel=…` | `routers/briefings.py:47-67` | hero card source |
| POST | `/api/v1/briefings/{channel}/generate` | `routers/briefings.py:70-83` | regenerate now |
| GET | `/api/v1/alerts` | `routers/alerts.py:14-32` | filterable |
| POST | `/api/v1/alerts/{id}/{acknowledge,resolve}` | `routers/alerts.py:35-60` | state machine |
| GET | `/api/v1/recommendations` | `routers/recommendations.py:14-32` | |
| POST | `/api/v1/recommendations/{id}/{approve,dismiss}` | `routers/recommendations.py:35-69` | approve auto-dispatches workflows |
| GET | `/api/v1/agents` | `routers/agents.py:14-26` | from in-memory registry |
| GET | `/api/v1/agents/runs` | `routers/agents.py:29-44` | |
| GET | `/api/v1/workflows` | `routers/workflows.py:23-26` | from registry |
| GET | `/api/v1/workflows/runs` | `routers/workflows.py:29-43` | history |
| POST | `/api/v1/workflows/run` | `routers/workflows.py:46-51` | trigger by name |
| GET | `/api/v1/integrations` | `routers/integrations.py:13-30` | + "configured" boolean |
| POST | `/api/v1/integrations/{provider}/ingest` | `routers/integrations.py:33-40` | manual trigger |
| GET | `/api/v1/infra/incidents` | `routers/infra.py:14-32` | |
| GET | `/api/v1/infra/incidents/{id}` | `routers/infra.py:35-40` | |
| POST | `/api/v1/copilot/ask` | `routers/copilot.py:34-93` | NL command bar |

### 9.2 Schemas

`api/schemas/__init__.py` defines Pydantic v2 response models with
`from_attributes=True` so SQLAlchemy rows serialise directly. The
frontend mirrors these exactly in `frontend/lib/api.ts:121-243` —
adding a new field on the backend means adding it on the TS interface
in one place.

### 9.3 Copilot

`/api/v1/copilot/ask` (`routers/copilot.py`) is the AI command bar at
the top of every page and the right-panel chat. Phase-1 implementation:

1. Pull the 40 most recent events + 20 open alerts from Postgres
   (`copilot.py:39-67`).
2. Build a flat text context.
3. Call `complete()` with a system prompt that grounds the LLM in
   "you are the operational copilot for Daalu" (`copilot.py:17-23`).
4. Return `{answer, references}` where references link back to the
   events that informed the answer.

Phase-2 will swap this for tool-use so the model can directly call
into other endpoints (`/infra/incidents`, `/sot/devices`, etc.)
rather than reading a pre-baked context.

---

## 10 · Data model

`src/daalu_automation/models/`. Every table inherits the three mixins
in `models/_mixins.py:17-46`:

- `UUIDPrimaryKeyMixin` — `id: UUID = uuid4()`
- `TenantScopedMixin` — `tenant_id: UUID` FK to `tenants.id`, defaults
  to `DEFAULT_TENANT_ID`
- `TimestampMixin` — `created_at` / `updated_at`

### 10.1 Platform tables

| Table | Source | Purpose |
|---|---|---|
| `tenants` | `models/tenant.py` | One row per operator org |
| `users` | `models/user.py` | Phase-2 auth target |
| `events` | `models/event.py` | Append-only history |
| `alerts` | `models/alert.py` | Agent-promoted alerts |
| `recommendations` | `models/recommendation.py` | Agent-promoted actions |
| `briefings` | `models/briefing.py` | AI-rendered reports |
| `agent_runs` | `models/agent_run.py` | One row per `Agent.handle()` |
| `workflow_runs` | `models/workflow_run.py` | One row per `run_workflow()` |
| `integrations` | `models/integration.py` | Per-tenant integration config |

### 10.2 Module tables

| Module | Tables | Source |
|---|---|---|
| infra | `infra_services`, `infra_incidents` | `models/infra.py` |

New modules add their own tables under `models/<name>.py` and
re-export from `models/__init__.py`. The `database.create_tables()`
hook (`database.py:42-55`) picks them up automatically at API boot —
production runs alembic instead via the init container in
`deploy/k8s/api/deployment.yaml:39-61`.

---

## 11 · End-to-end trace

### 11.1 Trace — Prometheus alert → triaged incident

```
00:00      Alertmanager webhook POSTs to /api/v1/events with
           type=infra.alert.fired, severity=critical

00:00.005  publish() → Event row + XADD

00:00.010  InfraAgent.handle() → _triage_alert()
                                  (modules/infra/agent.py:37-101)

00:00.020  complete_json(system=INCIDENT_TRIAGE_SYSTEM,
                          tier="quality") → Anthropic call

00:01.500  LLM returns {root_cause, remediation,
                         blast_radius: "high", confidence: 0.82}

00:01.510  Incident row written with ai_root_cause, ai_remediation,
           severity=sev1, status=open
                                  (modules/infra/agent.py:65-82)

00:01.520  emit_alert(severity="critical") + emit_recommendation
                                  ("Escalate Sev-1: …")

00:01.525  UI right-panel notifications stream picks it up
           (refetchInterval=15s)
           Recommendations page shows the escalation card

  ─── user clicks Approve on the escalation ───
  (today this just marks approved; Phase-2 would auto-page)

  ─── morning briefing ───

07:00      InfraBriefingGenerator surfaces this incident under
           "Active incidents" with the AI root cause.
```

---

## 12 · Frontend ↔ backend wiring

`frontend/lib/api.ts` is the only network surface from the UI. All
routes go through the typed `api` object. The Next dev server proxies
`/api/*` to the backend (`frontend/next.config.js:6-13`); in
production the same prefix is served by the same ingress
(`deploy/k8s/frontend/ingress.yaml:21-30`).

### 12.1 Where each card pulls from

| Frontend component | Backend endpoint(s) | Refresh strategy |
|---|---|---|
| `HeroBriefing` | `GET /briefings/latest?channel=…` + `POST /briefings/{ch}/generate` | on-mount, on-click |
| `EventFeedCard` | `GET /events/stream` (SSE) | live |
| `AIInsightCard` | static props from page | n/a |
| `AIRecommendationList` | `GET /recommendations?status=pending` + `POST /recommendations/{id}/{approve,dismiss}` | 20s polling |
| `MetricsCard` | derived from `useQuery` results on the page | n/a |
| `WorkflowStatusCard` | `GET /workflows/runs` | 15s polling |
| `RightPanel` Notifications | `GET /alerts?status=open` | 15s polling |
| `RightPanel` Active workflows | `GET /workflows/runs` | 10s polling |
| `CommandBar` | `POST /copilot/ask` | per-keystroke (Enter) |

Long-poll intervals are intentionally varied to avoid synchronised
spikes — the cheapest call (workflows summary) refreshes most often,
LLM-touched calls go last.

### 12.2 Pages

| Path | Source | Behaviour |
|---|---|---|
| `/` | `frontend/app/page.tsx` | Hero briefing + AI insight + recommendations + live feed + workflow status |
| `/operations` | `app/operations/page.tsx` | Filterable by module (infra / disabled stubs) |
| `/agents` | `app/agents/page.tsx` | Registry-driven workforce dashboard with success-rate per agent |
| `/automations` | `app/automations/page.tsx` | Workflows list + recent runs table; trigger any workflow |
| `/reports` | `app/reports/page.tsx` | Markdown briefings rendered with `react-markdown` + GFM |
| `/alerts` | `app/alerts/page.tsx` | Three tabs: open / acknowledged / resolved |
| `/integrations` | `app/integrations/page.tsx` | Registry-driven; "configured" chip from required-settings check |

### 12.3 Design tokens

Defined once in `frontend/tailwind.config.ts:8-44`. The `.gradient-border`
class in `frontend/app/globals.css:33-54` is the "hero treatment"
applied to the briefing card and any AI-headline card.

---

## 13 · Failure modes & recovery

| What can go wrong | What happens | Where to look |
|---|---|---|
| Anthropic 429 / outage | Briefing falls back to event-bullet body; agent triage uses heuristic; copilot returns canned "offline" message | `core/llm.py:34-37`; `core/briefings.py:163-172`; `modules/infra/agent.py` |
| Redis stream lost | Recently published events are lost; agent consumer-groups reset on restart; everything in Postgres survives | `core/events.py` — events table is the system of record |
| Agent crashes mid-handle | `AgentRun` row marked `status=error` with traceback; the event stays unacked, so it gets retried on next loop iteration | `core/agents.py:71-99` |
| Workflow raises | `WorkflowRun` marked `status=failed` with `error_message`; visible on Automations page | `core/workflows.py:54-69` |
| Slack webhook down | Briefing UI surface still works; logged at WARN | `core/notify.py:18-32` |
| Schema drift in dev | `create_tables()` is additive only — drop the DB and rerun `daalu seed` to reset | `database.py:42-55` |
| Schema drift in prod | Alembic init container runs `alembic upgrade head` before API container starts | `deploy/k8s/api/deployment.yaml:39-61` |

### 13.1 Observability hooks

`observability.py:14-44` wires structlog (JSON in prod, console in
dev) and conditionally enables Sentry when `SENTRY_DSN` is set. Every
agent/workflow log call is bound to the agent name or workflow name —
grepping `agent=infra-agent` in production logs shows the full
activity trail for that worker.

---

## 14 · Module extension contract

When you ship a new module (Support, Finance, Operations, HR,
Healthcare, Manufacturing — the six others in the original spec), you
add **one package** under `modules/<name>/`. The platform requires:

1. `module/__init__.py` does side-effect imports of every submodule so
   registrations fire.
2. Add the module name to `modules/__init__.py:14` so the platform's
   top-level import covers it.
3. (Optional) Add a value to `BriefingChannel` enum
   (`models/briefing.py:18-29`) if the briefing should be selectable
   in the UI dropdown.
4. (Optional) Add Celery tasks + a beat schedule entry in
   `workers/celery_app.py:43-79`.

The agent host, scheduler, API, and frontend pick up everything else
through their registries — no other platform code changes needed. See
[`extending.md`](extending.md) for the full step-by-step.

---

## 15 · Quick reference — file index

```
src/daalu_automation/
├── config.py                       — Settings (Pydantic) + DEFAULT_TENANT_ID
├── database.py                     — Async engine + AsyncSessionLocal + create_tables
├── observability.py                — structlog + Sentry init
├── cli.py                          — daalu {server, worker, beat, agents, seed, …}
│
├── core/
│   ├── events.py                   — EventEnvelope, publish, subscribe
│   ├── agents.py                   — Agent base, registry, emit_alert/recommendation
│   ├── briefings.py                — BriefingGenerator base, registry
│   ├── integrations.py             — IntegrationAdapter base, registry
│   ├── workflows.py                — register_workflow, run_workflow
│   ├── llm.py                      — complete, complete_json, LLMUnavailable
│   └── notify.py                   — send_slack, send_email
│
├── models/
│   ├── _mixins.py                  — UUIDPrimaryKey, TenantScoped, Timestamp
│   ├── tenant.py / user.py
│   ├── event.py / alert.py / recommendation.py / briefing.py
│   ├── agent_run.py / workflow_run.py / integration.py
│   └── infra.py                    — Service, Incident
│
├── api/
│   ├── main.py                     — FastAPI app factory + lifespan
│   ├── deps.py                     — current_tenant_id
│   ├── schemas/__init__.py         — Pydantic response models
│   └── routers/{events, briefings, alerts, recommendations,
│                 agents, workflows, integrations,
│                 infra, copilot}.py
│
├── modules/
│   └── infra/
│       ├── prompts.py              — INCIDENT_TRIAGE_SYSTEM, BRIEFING_SYSTEM
│       ├── briefing.py             — InfraBriefingGenerator
│       ├── agent.py                — InfraAgent
│       ├── integrations.py         — Synthetic, Prometheus, PagerDuty
│       ├── workflows.py            — infra.incident.coordinate
│       └── tasks.py                — celery wrappers
│
└── workers/
    ├── celery_app.py               — Celery + beat schedule
    └── agent_runner.py             — `daalu agents` host
```

If you can read this file index and the trace examples in §11, you
understand the platform.
