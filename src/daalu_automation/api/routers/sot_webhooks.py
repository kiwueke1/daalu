"""Nautobot webhook receiver.

Nautobot signs each outgoing webhook with HMAC-SHA512 under a
per-webhook secret that we mint at integration-setup time and persist
encrypted on the tenant's ``Integration(provider="nautobot")`` row.
This endpoint verifies the signature and then drops a
``sot.intent.changed`` event onto the existing event stream so the
reconciler / engine can react.

URL shape: ``POST /api/v1/sot/webhooks/{tenant_slug}``. The tenant
slug is the same one used to log into the UI; it travels in the path
so we don't need an ingest-key roundtrip.

The middleware in ``api/main.AuthGateMiddleware`` exempts this path
from the cookie/Bearer gate — same pattern used for the cluster
bootstrap callback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.crypto import decrypt_secret
from daalu_automation.core.events import EventEnvelope, publish
from daalu_automation.core.sot.nautobot import NAUTOBOT_PROVIDER
from daalu_automation.database import get_db
from daalu_automation.models import Integration, Tenant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sot", tags=["sot"])


@router.post("/webhooks/{tenant_slug}", status_code=202)
async def sot_webhook(
    tenant_slug: str,
    request: Request,
    x_hook_signature: str | None = Header(default=None, alias="X-Hook-Signature"),
    db: AsyncSession = Depends(get_db),
):
    # Resolve tenant by slug. 404 on miss (not 401) — we want the
    # response to be indistinguishable between "wrong slug" and
    # "right slug, wrong secret" so an attacker can't enumerate
    # tenants by webhook URL.
    tenant = (
        await db.execute(
            select(Tenant).where(
                Tenant.slug == tenant_slug,
                Tenant.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "tenant not found")

    row = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant.id,
                Integration.provider == NAUTOBOT_PROVIDER,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "nautobot integration not configured")
    # Accept both shapes: encrypted ciphertext from operator/provisioning
    # and plaintext from the onboarding wizard's BYO form. Prefer
    # encrypted when both are set (operator override on top of wizard).
    cfg = row.config or {}
    secret_ct = cfg.get("webhook_secret_ciphertext")
    secret_pt = cfg.get("webhook_secret")
    if not (secret_ct or secret_pt):
        # 409 because the integration row exists but is half-configured —
        # operator visible "you forgot to set a webhook secret" rather
        # than "tenant not found".
        raise HTTPException(409, "webhook secret not set")
    secret = (
        decrypt_secret(secret_ct).encode("utf-8")
        if secret_ct
        else secret_pt.encode("utf-8")
    )

    body = await request.body()
    expected = hmac.new(secret, body, hashlib.sha512).hexdigest()
    if not x_hook_signature or not hmac.compare_digest(expected, x_hook_signature):
        raise HTTPException(401, "invalid signature")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "body is not valid JSON")

    summary = _summarize(payload)
    await publish(
        EventEnvelope(
            tenant_id=str(tenant.id),
            type="sot.intent.changed",
            module="sot",
            source="nautobot",
            severity="info",
            summary=summary,
            payload=payload,
        )
    )
    return {"accepted": True}


def _summarize(payload: dict) -> str:
    """Build a one-liner for the events feed.

    Nautobot's webhook body typically carries ``event`` (created/updated/deleted),
    ``model`` and an ``data`` dict; fall back to a generic line if either
    is missing.
    """
    event = payload.get("event", "changed")
    model = payload.get("model") or payload.get("data", {}).get("display") or "?"
    data = payload.get("data") or {}
    name = (
        data.get("name")
        or data.get("display")
        or data.get("id")
        or "?"
    )
    return f"nautobot {model} {event}: {name}"
