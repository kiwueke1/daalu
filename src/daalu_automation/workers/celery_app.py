"""Celery app + beat schedule.

Importing this module also imports every module package so module-level
``@celery_app.task`` decorators take effect.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from daalu_automation.config import get_settings

settings = get_settings()

celery_app = Celery(
    "daalu_automation",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
)


# ── Task discovery — import side-effects register the @task decorators ──
def _autoload_modules() -> None:
    """Lazy import so the celery CLI can boot the app without circulars."""
    import daalu_automation.modules.infra.tasks  # noqa: F401
    import daalu_automation.workers.executor  # noqa: F401
    import daalu_automation.workers.integration_health  # noqa: F401
    import daalu_automation.workers.reconciler  # noqa: F401
    import daalu_automation.workers.report_dispatch  # noqa: F401
    import daalu_automation.workers.wireguard_health  # noqa: F401


_autoload_modules()


# ── Queue routing ────────────────────────────────────────────────────────
# The executor task is routed to a dedicated queue so the main worker
# pool — which consumes the default "celery" queue — physically cannot
# pick it up. That makes "is this pod the executor?" a question of
# Celery subscription, not just env var, and means an accidentally
# mis-deployed worker can't smuggle execute-rights into its own pod.
celery_app.conf.task_routes = {
    "sot.execute_approved": {"queue": settings.executor_queue_name},
}


# ── Beat schedule ───────────────────────────────────────────────────────
def _parse_cron(expr: str) -> crontab:
    minute, hour, dom, month, dow = expr.split()
    return crontab(minute=minute, hour=hour, day_of_month=dom, month_of_year=month, day_of_week=dow)


celery_app.conf.beat_schedule = {
    "infra-briefing": {
        "task": "infra.generate_briefing",
        "schedule": _parse_cron(settings.daily_briefing_cron),
    },
    # 30s — the UI shows a green/yellow/red badge keyed off
    # ClusterTunnel.status, and we want it to flip within ~1× window
    # after a peer goes silent.
    "wireguard-tunnel-health": {
        "task": "wireguard.poll_tunnel_health",
        "schedule": 30.0,
    },
    # 60s — poll CloudWatch alarms across every tenant with an AWS
    # integration row. Adapter no-ops silently for tenants that
    # haven't configured AWS. See AWSCloudWatchAlarmAdapter in
    # modules/infra/integrations.py.
    "aws-cloudwatch-alarm-ingest": {
        "task": "infra.monitoring_ingest",
        "schedule": 60.0,
        "args": ("aws",),
    },
    # Pull active firing alerts from each tenant's Alertmanager (the
    # `prometheus` integration row) and emit `infra.alert.fired` events
    # so the InfraAgent promotes them to Alert rows on the Alerts page.
    # Tenants without a prometheus integration are no-ops. Without this
    # the Alertmanager alerts never enter daalu (the integration being
    # "connected" only means the health probe passes). See
    # PrometheusAdapter.ingest in modules/infra/integrations.py.
    "prometheus-alert-ingest": {
        "task": "infra.monitoring_ingest",
        "schedule": float(settings.prometheus_ingest_period_s),
        "args": ("prometheus",),
    },
    # SoT drift detection. Iterates every tenant with both nautobot
    # and ssh_credentials configured, walks Linux devices, opens
    # ChangeProposal(kind=drift) rows on divergence. Tenants
    # without the integrations are silently skipped.
    "sot-reconcile-devices": {
        "task": "sot.reconcile_devices",
        "schedule": float(settings.sot_reconcile_period_s),
    },
    # 30s — drains the approved-but-not-yet-executed ChangeProposal
    # queue. Routed via task_routes above to the executor queue, so
    # only the daalu-executor Deployment will actually run it. Cadence
    # bounds the approve→push latency; humans approve, so a few tens
    # of seconds is fine.
    "sot-execute-approved": {
        "task": "sot.execute_approved",
        "schedule": float(settings.executor_period_s),
    },
    # 60s — find any ReportSchedule whose next_run_at has passed,
    # render its SavedReport, and deliver to Slack / email. Cheap when
    # no schedules are due (one indexed query). See
    # workers/report_dispatch.py.
    "reports-dispatch-schedules": {
        "task": "reports.dispatch_schedules",
        "schedule": 60.0,
    },
    # 60s — probe every Integration row's endpoint (Prometheus
    # /-/healthy, PagerDuty /abilities, etc.) and flip
    # Integration.status to error if the probe fails. The UI's
    # green/red badge keys off this column, so the badge reflects
    # current liveness without the operator having to do anything.
    # See workers/integration_health.py.
    "integrations-health-check": {
        "task": "integrations.health_check",
        "schedule": 60.0,
    },
}
