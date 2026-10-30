"""Per-tenant integration config resolver.

Replaces the platform-wide ``get_settings()`` reads scattered through
notify / briefings / adapters / workflows. Each tenant has zero or more
rows in the ``integrations`` table keyed by ``provider``; the row's
``config: JSON`` column carries that tenant's URL, credentials, channel
overrides, etc.

Resolution order is **tenant row → environment default → empty**. The
environment defaults exist so a single-tenant deployment can still
populate everything via ``.env`` (Phase-1 ergonomics) — the moment a
tenant adds an integration row for the same provider, the row wins.
A missing row + missing env default means the channel is disabled for
that tenant (e.g. ``send_slack`` returns False), not that it falls
through to another tenant's credentials.

Caching. Each ``TenantConfig`` snapshot is built fresh on each call so
config changes propagate without a restart. Hot paths (Slack/email
under firehose alert load) should cache at the caller — typical alert
rates are << 1/s so the JSON column read isn't a real concern.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import get_settings
from daalu_automation.models import Integration

# Provider slugs used in the ``integrations.provider`` column. Module
# code should reference these constants rather than literal strings so
# typos surface at import time.
PROVIDER_SLACK = "slack"
PROVIDER_SMTP = "smtp"
PROVIDER_PROMETHEUS = "prometheus"
PROVIDER_PAGERDUTY = "pagerduty"


@dataclass(slots=True)
class TenantConfig:
    """All per-tenant integration settings, resolved at point of use."""

    tenant_id: uuid.UUID

    # Slack
    slack_webhook_url: str = ""
    slack_briefing_channel: str = "#operations"
    slack_incidents_channel: str = ""  # blank → use briefing channel

    # Email (SMTP)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    incident_email_to: str = ""

    # Infra integrations
    prometheus_url: str = ""
    pagerduty_api_token: str = ""

    def has_slack(self) -> bool:
        return bool(self.slack_webhook_url)

    def has_email(self) -> bool:
        return bool(self.smtp_host)


async def get_tenant_config(
    db: AsyncSession, tenant_id: uuid.UUID
) -> TenantConfig:
    """Resolve every integration's config for a tenant.

    Falls back to ``Settings``-defined env defaults for any field whose
    matching ``integrations`` row is absent or empty. Returns a fresh
    snapshot — safe to mutate by the caller, never written back.
    """
    settings = get_settings()
    cfg = TenantConfig(
        tenant_id=tenant_id,
        slack_briefing_channel=settings.slack_briefing_channel,
        smtp_port=settings.smtp_port,
        smtp_from=settings.smtp_from,
    )

    rows = (
        await db.execute(
            select(Integration).where(Integration.tenant_id == tenant_id)
        )
    ).scalars().all()
    by_provider: dict[str, dict[str, Any]] = {
        row.provider: row.config or {} for row in rows
    }

    slack = by_provider.get(PROVIDER_SLACK, {})
    cfg.slack_webhook_url = slack.get("webhook_url") or settings.slack_webhook_url
    cfg.slack_briefing_channel = (
        slack.get("briefing_channel") or settings.slack_briefing_channel
    )
    cfg.slack_incidents_channel = slack.get("incidents_channel", "")

    smtp = by_provider.get(PROVIDER_SMTP, {})
    cfg.smtp_host = smtp.get("host") or settings.smtp_host
    cfg.smtp_port = int(smtp.get("port") or settings.smtp_port)
    cfg.smtp_username = smtp.get("username") or settings.smtp_username
    cfg.smtp_password = smtp.get("password") or settings.smtp_password
    cfg.smtp_from = smtp.get("from") or settings.smtp_from
    cfg.incident_email_to = smtp.get("incident_email_to", "")

    prom = by_provider.get(PROVIDER_PROMETHEUS, {})
    cfg.prometheus_url = prom.get("url") or settings.prometheus_url

    pd = by_provider.get(PROVIDER_PAGERDUTY, {})
    cfg.pagerduty_api_token = pd.get("api_token") or settings.pagerduty_api_token

    return cfg


def env_default_config(tenant_id: uuid.UUID) -> TenantConfig:
    """Build a TenantConfig from process-global env vars alone.

    Used by callers that have a ``tenant_id`` but no DB session handy
    (e.g. the Celery beat scheduler before it opens a session). Safe
    only for the bootstrap tenant — for any real tenant, integrations
    rows should exist and ``get_tenant_config`` is the right path.
    """
    settings = get_settings()
    return TenantConfig(
        tenant_id=tenant_id,
        slack_webhook_url=settings.slack_webhook_url,
        slack_briefing_channel=settings.slack_briefing_channel,
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_username=settings.smtp_username,
        smtp_password=settings.smtp_password,
        smtp_from=settings.smtp_from,
        prometheus_url=settings.prometheus_url,
        pagerduty_api_token=settings.pagerduty_api_token,
    )
