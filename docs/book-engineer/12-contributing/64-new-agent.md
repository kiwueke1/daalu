# 64. Adding a new agent

Agents are scheduled or trigger-based behaviours that run as
Celery tasks and surface on the *Agents* page. Examples
ship in the product: briefing, reconciler, alert-explainer,
cost-anomaly, cert-expiry.

This chapter walks through adding a new shipped agent.
For *customer-authored* automations (workflows), see Chapter
21 of the user guide.

---

## When to add a shipped agent vs a workflow

- **Shipped agent** — applies to every tenant, the logic is
  Daalu's, the configuration is per-tenant.
- **Workflow (customer authored)** — applies to one tenant,
  the logic is the customer's.

The bar for adding a shipped agent: it has to be useful to
the vast majority of tenants. Cost-anomaly is. A
custom-business-process agent isn't.

---

## 1. Pick the trigger

Most agents are scheduled (cron). Some are event-triggered
(react to an event matching a filter).

Cron schedule lives in `workers/beat_schedule.py`:

```python
beat_schedule = {
    ...
    "expiry-sweep-daily": {
        "task": "daalu.security.cert_expiry_sweep",
        "schedule": crontab(hour=2, minute=0),
    },
}
```

Event triggers live in `core/eventbus.py`:

```python
@on("event.created")
async def maybe_explain_alert(event: Event):
    if event.kind == "alert.fired":
        await celery_app.send_task(
            "daalu.alerts.explain",
            args=[event.payload["alert_id"]],
            queue="agents",
        )
```

---

## 2. Implement the task

The task itself is a regular Celery task:

```python
# workers/tasks/cert_expiry.py
from daalu_automation.workers.celery_app import celery_app

@celery_app.task(name="daalu.security.cert_expiry_sweep", bind=True)
def cert_expiry_sweep(self):
    tenants = enumerate_tenants_with_certs()
    for tenant in tenants:
        check_tenant_certs(tenant.id)
```

Don't do the work directly in the scheduled task. Fan out to
per-tenant or per-resource sub-tasks so concurrency works.

---

## 3. Surface on the Agents page

Each shipped agent is registered in `core/agents/registry.py`:

```python
AGENTS = {
    "briefing": AgentInfo(
        name="Daily briefing",
        task_name="daalu.briefing.generate",
        default_schedule="0 8 * * *",
        configurable_fields=["time", "modules"],
    ),
    "cert-expiry": AgentInfo(
        name="Certificate expiry sweep",
        task_name="daalu.security.cert_expiry_sweep",
        default_schedule="0 2 * * *",
        configurable_fields=["days_warning"],
    ),
    # ...
}
```

The `/agents` API reads this and renders the table.

---

## 4. Per-tenant configuration

If the agent has settings, model them as a JSONB column on
`agent_configs`:

```python
class AgentConfig(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    agent_name: Mapped[str]
    enabled: Mapped[bool] = mapped_column(default=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
```

Defaults come from `AGENTS[name].defaults`. The UI on the
agent's detail page edits this.

---

## 5. Run history

Every run writes to `agent_runs`:

```python
class AgentRun(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    agent_name: Mapped[str]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    status: Mapped[str]   # 'running', 'success', 'failure'
    duration_ms: Mapped[int | None]
    output_summary: Mapped[str | None]
    output_artifact_id: Mapped[UUID | None]
    error: Mapped[str | None]
```

The agent's task wraps its main work in
`record_agent_run`:

```python
@celery_app.task(name="daalu.security.cert_expiry_sweep", bind=True)
def cert_expiry_sweep(self):
    with record_agent_run("cert-expiry") as run:
        ... do work ...
        run.output_summary = f"checked {n} certs, {m} expiring"
```

`record_agent_run` is a context manager that creates the
`AgentRun` row, captures duration, captures exceptions.

---

## 6. Tests

Cover:

- Happy path: agent runs, output is correct, run is recorded.
- Failure: agent task raises; run is recorded with status
  failure.
- Per-tenant config: changing config affects behaviour.

---

## 7. Document

- Add a section to Chapter 19 of the user guide.
- Add to the shipped-agents list in the customer's onboarding
  if user-visible.

---

## 8. Submit

Checklist:

- Task implemented.
- Registry entry.
- AgentConfig row default if needed.
- Tests.
- Documentation in user guide.

---

## What's next

Chapter 65 — database migration patterns.
