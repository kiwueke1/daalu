"""Delivery channels for AI briefings + critical alerts.

Slack + email today; Teams/WhatsApp later. Every channel is best-effort
so a misconfigured customer SMTP server doesn't take down briefing
generation for that tenant — let alone other tenants.

Tenancy. Both senders take a ``tenant_id`` and resolve the tenant's
Slack webhook / SMTP relay from the ``integrations`` table via
``get_tenant_config``. There is **no fallback to a global webhook**:
if a tenant has no Slack integration row and the env default is empty,
``send_slack`` returns ``False`` and logs ``notify.slack_unconfigured``.
This is the correctness primitive that prevents tenant A's incident
from being posted to tenant B's Slack channel if tenant A's row is
accidentally missing.
"""

from __future__ import annotations

import smtplib
import uuid
from email.message import EmailMessage

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.tenant_settings import (
    TenantConfig,
    env_default_config,
    get_tenant_config,
)

logger = structlog.get_logger(__name__)


async def _resolve(
    db: AsyncSession | None,
    tenant_id: uuid.UUID | None,
    config: TenantConfig | None,
) -> TenantConfig | None:
    """Pick the tenant config the caller meant.

    Callers can pass an explicit ``config`` (test injection), a
    ``(db, tenant_id)`` pair (request-scoped path), or just a
    ``tenant_id`` (Celery / worker path — falls back to env defaults).
    """
    if config is not None:
        return config
    if tenant_id is None:
        return None
    if db is not None:
        return await get_tenant_config(db, tenant_id)
    return env_default_config(tenant_id)


async def send_slack(
    text: str,
    *,
    tenant_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
    config: TenantConfig | None = None,
    channel: str | None = None,
) -> bool:
    cfg = await _resolve(db, tenant_id, config)
    if cfg is None or not cfg.has_slack():
        logger.info(
            "notify.slack_unconfigured",
            tenant_id=str(tenant_id) if tenant_id else None,
        )
        return False
    body: dict = {"text": text}
    target_channel = channel or cfg.slack_briefing_channel
    if target_channel:
        body["channel"] = target_channel
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(cfg.slack_webhook_url, json=body)
            r.raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "notify.slack_failed", tenant_id=str(cfg.tenant_id)
        )
        return False


def _send_email_sync(cfg: TenantConfig, to: str, subject: str, body_markdown: str) -> bool:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_from
    msg["To"] = to
    msg.set_content(body_markdown)
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as s:
            s.starttls()
            if cfg.smtp_username:
                s.login(cfg.smtp_username, cfg.smtp_password)
            s.send_message(msg)
        return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "notify.email_failed", tenant_id=str(cfg.tenant_id)
        )
        return False


async def send_email(
    to: str | None,
    subject: str,
    body_markdown: str,
    *,
    tenant_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
    config: TenantConfig | None = None,
) -> bool:
    """Send an email via the tenant's SMTP relay.

    Async wrapper around a sync smtplib call so callers can use either
    style. If ``to`` is None, falls back to the tenant's
    ``incident_email_to`` config; if both are empty, returns False.
    """
    cfg = await _resolve(db, tenant_id, config)
    if cfg is None or not cfg.has_email():
        logger.info(
            "notify.email_unconfigured",
            tenant_id=str(tenant_id) if tenant_id else None,
        )
        return False
    recipient = to or cfg.incident_email_to
    if not recipient:
        logger.info(
            "notify.email_no_recipient", tenant_id=str(cfg.tenant_id)
        )
        return False
    import asyncio

    return await asyncio.to_thread(
        _send_email_sync, cfg, recipient, subject, body_markdown
    )
