"""Invite-token helpers + delivery.

Tokens are 32 random URL-safe bytes prefixed with ``dinv_`` so a
leaked secret is recognisable in logs / .env files. The cleartext
is shown to the inviter exactly once (embedded in the invite URL);
only the sha256 hash is stored in :class:`Invite.token_hash`.

Delivery: ``send_invite_email`` calls into :func:`core.notify.send_email`
which routes through the tenant's SMTP integration (Settings →
Integrations → Email) or the platform-level ``SMTP_*`` env defaults.
When no SMTP is configured the function returns ``False`` and logs
the invite URL — the inviter then sees the "copy link manually"
fallback banner.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from urllib.parse import urljoin

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import get_settings
from daalu_automation.core import notify

logger = structlog.get_logger(__name__)

_PREFIX = "dinv_"


def generate_invite_token() -> str:
    """Return a fresh invite-token cleartext.

    ``dinv_<24 random url-safe bytes>`` — same shape as PATs so the
    family is visually consistent.
    """
    return f"{_PREFIX}{secrets.token_urlsafe(24)}"


def hash_invite_token(token: str) -> str:
    """sha256 hex of the cleartext for storage / lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_invite_url(token: str) -> str:
    """Build the full accept-invite URL for the front-end.

    Uses ``settings.public_base_url`` if set (production); falls back
    to a path-relative URL in dev so the operator can prepend their
    own host.
    """
    settings = get_settings()
    base = getattr(settings, "public_base_url", "") or ""
    path = f"/accept-invite/{token}"
    if base:
        return urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    return path


def _render_invite_body(
    *,
    invite_url: str,
    inviter_name: str | None,
    tenant_name: str,
    role: str,
    message: str | None,
) -> str:
    """Plain-text email body. Markdown-flavoured so it reads well in
    a console preview, but most clients render it as plain text — both
    paths are legible."""
    inviter = inviter_name or "Your teammate"
    lines = [
        f"{inviter} invited you to join **{tenant_name or 'Daalu'}** on Daalu as a *{role}*.",
        "",
        "Accept the invite by clicking the link below:",
        invite_url,
        "",
        "This link is single-use and expires in 7 days. If you weren't",
        "expecting this email, you can safely ignore it.",
    ]
    if message:
        lines += ["", f"Message from {inviter}:", message]
    return "\n".join(lines)


async def send_invite_email(
    *,
    to_email: str,
    invite_url: str,
    inviter_name: str | None,
    tenant_name: str,
    role: str,
    message: str | None,
    tenant_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
) -> bool:
    """Deliver the invite to ``to_email``.

    Returns ``True`` if SMTP delivery succeeded, ``False`` if SMTP isn't
    configured or the send failed. On ``False`` the URL is logged at
    INFO so a small team can still copy + send manually — and the
    inviter's UI shows the "copy link" fallback banner.
    """
    subject = f"You're invited to {tenant_name or 'Daalu'}"
    body = _render_invite_body(
        invite_url=invite_url,
        inviter_name=inviter_name,
        tenant_name=tenant_name,
        role=role,
        message=message,
    )
    delivered = await notify.send_email(
        to=to_email,
        subject=subject,
        body_markdown=body,
        tenant_id=tenant_id,
        db=db,
    )
    if delivered:
        logger.info(
            "invite.delivered",
            to=to_email,
            inviter=inviter_name,
            tenant=tenant_name,
            role=role,
        )
        return True
    # SMTP not configured (or send failed). Surface the URL in the logs
    # so an operator running the stub setup can hand it off manually.
    logger.info(
        "invite.delivery_unconfigured",
        to=to_email,
        url=invite_url,
        inviter=inviter_name,
        tenant=tenant_name,
        role=role,
        message_truncated=(message or "")[:200],
        note=(
            "no SMTP integration on this tenant and no platform-level "
            "SMTP_* env defaults — invite URL above is what the invitee "
            "needs. Configure Settings → Integrations → Email to enable "
            "automatic delivery."
        ),
    )
    return False
