# 7. The Celery worker pool

Celery handles all background work in Daalu. There are three
worker classes: the **default worker pool**, **beat**
(scheduler), and **the executor**. Each is its own
Deployment in the Helm chart.

This chapter is about how they're configured, what they
each do, and how task routing works.

---

## Celery app

`src/daalu_automation/workers/celery_app.py` configures the
single `Celery()` instance imported by all worker
processes:

```python
celery_app = Celery(
    "daalu",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_url,
)

celery_app.conf.update(
    task_routes={
        "daalu.sot.execute_approved": {"queue": "executor"},
        "daalu.sot.reconcile": {"queue": "default"},
        "daalu.alerts.explain": {"queue": "agents"},
        # ...
    },
    task_default_queue="default",
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

celery_app.autodiscover_tasks(["daalu_automation.workers.tasks"])
```

The `task_routes` dict is the **identity gate** for the
executor — see the next subsection.

---

## Three worker classes

### Default worker pool (`daalu-worker`)

```bash
daalu worker --concurrency=8
```

Subscribes to `default` and `agents` queues. Handles:

- Reconciliation runs (for SoT drift detection).
- Cloud-account inventory crawls.
- Alert-explainer agent.
- Daily-briefing generator.
- Cost-anomaly checks.
- Inventory sync jobs.
- Most background work.

Multiple replicas by default (3 in production), scaled per
load.

### Beat (`daalu-beat`)

```bash
daalu beat
```

The scheduler. Reads `beat_schedule.py` and queues tasks at
their configured cron times.

**Single replica.** The Helm chart's comment is loud about
this: *"Beat MUST stay 1 — multiple schedulers fire duplicate
tasks."*

### Executor (`daalu-executor`)

```bash
daalu executor --concurrency=4
```

Subscribes **only** to the `executor` queue. The main worker
pool consumes the `default` queue and therefore physically
cannot pick up `sot.execute_approved`.

Why split? Two reasons:

1. **Authorization.** The executor's Pod has a different
   ServiceAccount with the credentials that can mutate
   external state. The main worker pool's SA is
   intentionally narrower.
2. **Defense in depth.** Even if a bug in the API let an
   un-approved proposal into a task, only the executor
   could run it. The queue routing is a physical barrier,
   not just a policy.

The chart's `templates/workers.yaml` enforces:

- `replicas: 1` (single executor identity per tenant).
- `serviceAccountName: daalu-executor` (separate SA).
- `automountServiceAccountToken: false`.
- The `EXECUTOR_JWT_SCOPE` env var pinned to `executor`.

---

## Task definitions

Tasks live under `src/daalu_automation/workers/tasks/`. Each
follows:

```python
from daalu_automation.workers.celery_app import celery_app

@celery_app.task(
    name="daalu.alerts.explain",
    bind=True,
    max_retries=3,
    retry_backoff=True,
)
def explain_alert(self, alert_id: str):
    try:
        ...
    except SomeRecoverableError as exc:
        raise self.retry(exc=exc)
```

The `name=` keyword is required — it's the canonical task
identifier the `task_routes` dict matches on.

---

## Beat schedule

`beat_schedule.py`:

```python
beat_schedule = {
    "daily-briefing-08am": {
        "task": "daalu.briefing.generate",
        "schedule": crontab(hour=8, minute=0),
    },
    "reconcile-every-5min": {
        "task": "daalu.sot.reconcile_all",
        "schedule": crontab(minute="*/5"),
    },
    "cost-anomaly-hourly": {
        "task": "daalu.billing.check_anomalies",
        "schedule": crontab(minute=0),
    },
    "cert-expiry-daily": {
        "task": "daalu.security.cert_expiry_sweep",
        "schedule": crontab(hour=2, minute=0),
    },
    # ...
}
```

The schedule fires tasks with no arguments. Tasks that need
arguments use a fan-out pattern: the scheduled task enumerates
the work and queues one sub-task per unit.

---

## Concurrency choices

- **Default worker:** 8 concurrent tasks per replica.
- **Executor:** 4. Slower throughput, but lower contention
  when applying to a single shared resource.
- **Beat:** N/A (no task execution, only scheduling).

Concurrency is gevent-pool by default; the worker process
runs `--pool=gevent` for I/O-heavy workloads. CPU-heavy tasks
(running an LLM, e.g.) are dispatched to the LLM router and
not run in-process.

---

## Retries and failures

Tasks declare their retry policy on the decorator:

- `max_retries=3, retry_backoff=True` — exponential backoff
  starting at ~3 seconds, capped at 600.
- `acks_late=True` for tasks where double-run is worse than
  missing-once (rare; default is fire-and-forget).

Failed tasks land in the Celery result backend (Redis) with
their traceback. The dashboard at `daalu-api → /api/v1/
admin/tasks` (superuser only) shows them.

---

## Idempotency

Many tasks are inherently idempotent (reconcile a device
twice → same result). For tasks that mutate state, the
executor checks idempotency by:

- **Proposal lifecycle.** A proposal can only be in `approved`
  state once; the executor transitions it to `applied`
  atomically.
- **Idempotency keys on external API calls.** Where the
  external system supports it (AWS, Stripe, GitHub),
  Daalu sends an idempotency key derived from the proposal
  ID.

---

## Observability

Each task logs via structlog with bound context:

- `task_name`
- `task_id` (Celery's UUID)
- `tenant_id`
- `correlation_id` (if available)

Metrics are emitted via prometheus-client:

- `celery_task_duration_seconds` (histogram, labelled by
  task_name).
- `celery_task_total{state=success|failure|retry}`.
- `celery_queue_depth{queue=...}` (sampled by the
  daalu-agents pod's monitoring sidecar).

The kube-prom-stack picks up the workers' /metrics via the
`daalu-workers` PodMonitor — see Chapter 39.

---

## What's next

Chapter 8 — the module system, which is where domain code
beyond Celery tasks lives.
