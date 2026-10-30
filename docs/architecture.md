# Architecture

Daalu Automation is an **event-driven operational intelligence platform**.
The core insight: most enterprise inefficiency is *coordination overhead*
— humans manually checking dashboards, asking for updates, drafting
status reports, chasing follow-ups. The platform compresses that work
by continuously consuming events, reasoning over them with an LLM, and
emitting briefings, alerts, recommendations, and automated workflows.

## The category

We sit at the intersection of several markets that today are fragmented:

| Market | What they do |
|---|---|
| Workflow Automation | Move data/actions between systems |
| Business Process Automation | Automate repetitive workflows |
| AI Agents for Operations | AI gathers context and executes tasks |
| Operational Intelligence | Detect issues, summarise state |
| Internal Reporting Automation | Auto-generate briefings |
| Enterprise AI Copilot | NL queries over the company state |

The product positioning is **"AI Coordination Layer for Enterprise
Operations"** — not a dashboard, not a chatbot.

---

## High-level architecture

```
Company Systems
  ├── Monitoring (Prometheus, Datadog)
  ├── Ticketing (PagerDuty, Jira)
  ├── Source of Truth (Nautobot)
  ├── Messaging (Slack, Email)
  └── …
        ↓
Integration Layer  (src/daalu_automation/core/integrations.py)
  - adapters pull state on a schedule + emit events
  - or receive pushes via /events webhook
        ↓
Event Bus  (Redis Streams — src/daalu_automation/core/events.py)
  - typed envelopes (module + source + type + payload)
  - consumer-group fan-out so workers scale horizontally
        ↓
Operational State  (Postgres)
  - events: append-only history
  - alerts / recommendations / briefings: derived rows
  - module tables: incidents, services, devices …
        ↓
AI Reasoning Engine  (src/daalu_automation/core/llm.py)
  - Anthropic Claude as the primary "quality" tier
  - any OpenAI-compatible endpoint as the cheaper "classifier" tier
  - Prompt caching enabled — large system prompts only pay once
        ↓
Agents + Briefings + Workflows  (per-module packages)
  - agents subscribe to event types and react
  - briefings render markdown reports on a schedule
  - workflows orchestrate multi-step automations
        ↓
Action Layer
  - Slack / Email / WhatsApp deliveries
  - Device config writes, ticket updates
  - Human-in-the-loop approval gates
```

Every layer is replaceable. The Phase-1 event bus is Redis Streams; a
Phase-2 deployment can swap in Kafka or NATS without touching modules.
The Phase-1 workflow runner is in-process; a Phase-2 deployment can
swap in Temporal.

---

## Core primitives

All under `src/daalu_automation/core/`:

| File | What it owns |
|---|---|
| `events.py` | `EventEnvelope`, `publish()`, `subscribe()` over Redis streams |
| `agents.py` | `Agent` base class, `register_agent()`, `emit_alert/recommendation` |
| `briefings.py` | `BriefingGenerator` base, registry, persistence + AI call |
| `integrations.py` | `IntegrationAdapter` base, registry |
| `workflows.py` | `register_workflow()` + `run_workflow()` (in-process runner) |
| `llm.py` | Unified Anthropic + OpenAI-compatible client |
| `notify.py` | Slack + email delivery |

Modules **only depend on `core`** — never on each other. This keeps the
blast radius of any one module small and means new modules can be added
without modifying existing code.

---

## Module anatomy

A module is a Python package under `src/daalu_automation/modules/<name>/`
that provides:

- `__init__.py` — side-effect imports that register everything
- `briefing.py` — a `BriefingGenerator` subclass
- `agent.py` — one or more `Agent` subclasses
- `integrations.py` — `IntegrationAdapter` subclasses (always include a
  synthetic adapter so demos work without external credentials)
- `workflows.py` — `@register_workflow`-decorated coroutines
- `prompts.py` — LLM prompts kept in one place
- `tasks.py` — Celery task wrappers around the briefing + scheduled jobs

The infra module is the canonical example. New modules follow the
same shape — see [extending.md](extending.md).

---

## Data model

```
tenants ─── users
       │
       ├── events  (append-only event stream)
       │     ├── alerts          (promoted by agents)
       │     ├── recommendations (promoted by agents)
       │     └── briefings       (daily AI-rendered summaries)
       │
       ├── agent_runs            (one row per agent invocation)
       ├── workflow_runs         (one row per workflow execution)
       ├── integrations          (per-tenant integration config + status)
       │
       └── infra_services ── infra_incidents                       [infra module]
```

Every operational row carries `tenant_id`. Phase-1 ships single-tenant
(rows pin to `DEFAULT_TENANT_ID`); Phase-2 turns on Postgres
row-level security.

---

## Event taxonomy

Events follow `<module>.<noun>.<verb>` naming, e.g.:

| Event type | Emitted by | Consumed by |
|---|---|---|
| `infra.alert.fired` | Prometheus | InfraAgent (triage) |
| `infra.alert.resolved` | Prometheus | InfraAgent |
| `infra.incident.opened` | PagerDuty / agent | briefing |
| `infra.deployment.failed` | ArgoCD | InfraAgent (sev1) |
| `infra.capacity.warning` | capacity-planner | InfraAgent (recommends) |

New modules invent their own namespace. No central registry — the
"contract" is just the prefix.

---

## Why this shape

A few decisions worth understanding before extending:

**Events first, derived state second.** Every alert, recommendation, and
briefing is *derived* from an immutable event log. This means we can:
re-render briefings (the LLM prompt changed); replay events into a new
module; debug "why did this alert fire" by reading the source events.

**LLM at the edge, not the centre.** The platform doesn't pipe every
event through an LLM. The bus is plain Redis Streams + Postgres; LLM
calls happen lazily, batched at briefing time or when an agent decides
an event is interesting enough. This keeps cost bounded and makes
latency predictable.

**Two LLM tiers.** Quality (Anthropic Sonnet) for synthesis and triage;
classifier (anything OpenAI-compatible — DeepSeek, Together, …) for
high-volume scoring. Same `complete()` signature, picked per-call via
`tier=`.

**Mock integrations always present.** Every module ships a
"synthetic-*" adapter so the platform is demo-able with zero external
credentials. This also makes integration tests deterministic.

**One image, three entrypoints.** The same container runs the API
(`daalu server`), Celery worker (`daalu worker`), beat scheduler
(`daalu beat`) and agent host (`daalu agents`). Different `command:`
values in `deploy/k8s/`.

---

## Roadmap

Phase 1 (this branch):
- ✅ Event bus, agents, briefings, integrations
- ✅ Infra module
- ✅ Dark "operational nervous system" UI
- ✅ K8s deployment with networkpolicies

Phase 2:
- Multi-tenant Postgres RLS
- Temporal-backed workflow runner
- Keycloak OIDC auth
- Vector memory for the copilot (`pgvector`)

Phase 3:
- Support, Finance, Operations, HR modules
- Tool-use for the copilot (agents callable from natural language)
- Programmatic workflow builder UI (n8n-style canvas)
