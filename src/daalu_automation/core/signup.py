"""Self-service local signup — create an isolated account from email+password.

A visitor who signs up gets their **own empty tenant** and a single
non-admin :class:`User` inside it (full data isolation — they can never
see another tenant's rows). The account is created **inactive**; an
email-verification link must be redeemed before it can sign in. This
module is the orchestration the ``/auth/signup`` + ``/auth/verify-email``
routes call, kept out of the router so it's unit-testable.

Token model mirrors ``core/invites.py``: the cleartext (``dver_<random>``)
only ever lives in the emailed link; the DB stores its sha256 hash.
Delivery reuses ``core.notify.send_email`` (tenant SMTP, or the
platform-level ``SMTP_*`` defaults for a brand-new tenant with no SMTP of
its own); when email is unconfigured it returns ``False`` and the link is
logged so a dev can copy it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import get_settings
from daalu_automation.core import notify
from daalu_automation.core.auth import hash_password
from daalu_automation.models import EmailVerificationToken, Tenant, User

logger = structlog.get_logger(__name__)

_PREFIX = "dver_"


def generate_verification_token() -> str:
    """Return a fresh verification-token cleartext (``dver_<random>``)."""
    return f"{_PREFIX}{secrets.token_urlsafe(24)}"


def hash_verification_token(token: str) -> str:
    """sha256 hex of the cleartext for storage / lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_verify_url(token: str) -> str:
    """Full ``/verify-email?token=…`` URL for the front-end.

    Uses ``settings.public_base_url`` in production; falls back to a
    path-relative URL in dev so the operator can prepend their own host.
    """
    settings = get_settings()
    base = getattr(settings, "public_base_url", "") or ""
    path = "/verify-email?" + urlencode({"token": token})
    if base:
        return urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    return path


def _slugify_email(email: str) -> str:
    """Derive a tenant-slug stem from an email local part.

    Collision-resistance is provided by the random suffix the caller
    appends — this is only the human-readable stem.
    """
    local = email.split("@", 1)[0].lower()
    stem = re.sub(r"[^a-z0-9]+", "-", local).strip("-") or "user"
    return stem[:32]


async def create_self_service_account(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str | None = None,
    organization: str | None = None,
) -> tuple[User, str]:
    """Create an isolated tenant + inactive non-admin user + verification token.

    Returns ``(user, cleartext_token)``. The caller is responsible for
    sending the email (so it can decide on neutral responses / logging).
    Everything here is one transaction — committed before return so the
    token row is durable when the email goes out.

    Security invariants enforced here (never taken from request input):
    ``is_admin=False``, ``is_superuser=False``, ``is_active=False`` until
    the email is verified, and a brand-new ``tenant_id`` per signup.
    """
    norm = email.strip().lower()
    settings = get_settings()

    tenant = Tenant(
        name=(organization or "").strip() or norm,
        slug=f"{_slugify_email(norm)}-{secrets.token_hex(3)}",
        timezone="UTC",
    )
    db.add(tenant)
    await db.flush()  # assign tenant.id

    user = User(
        email=norm,
        full_name=(full_name or "").strip() or None,
        hashed_password=hash_password(password),
        is_active=False,  # gated on email verification
        is_admin=False,
        is_superuser=False,
        tenant_id=tenant.id,
    )
    db.add(user)
    await db.flush()  # assign user.id

    cleartext = generate_verification_token()
    token = EmailVerificationToken(
        user_id=user.id,
        token_hash=hash_verification_token(cleartext),
        expires_at=datetime.now(tz=timezone.utc)
        + timedelta(hours=settings.signup_token_expire_hours),
    )
    db.add(token)
    await db.commit()
    await db.refresh(user)
    logger.info("signup.account_created", email=norm, tenant_id=str(tenant.id))
    return user, cleartext


async def issue_verification_token(db: AsyncSession, user: User) -> str:
    """Mint a fresh verification token for an existing unverified user (resend).

    Returns the cleartext. Old tokens are left in place (they expire on
    their own); a resend is simply a new row.
    """
    settings = get_settings()
    cleartext = generate_verification_token()
    token = EmailVerificationToken(
        user_id=user.id,
        token_hash=hash_verification_token(cleartext),
        expires_at=datetime.now(tz=timezone.utc)
        + timedelta(hours=settings.signup_token_expire_hours),
    )
    db.add(token)
    await db.commit()
    return cleartext


async def send_verification_email(
    *,
    to_email: str,
    verify_url: str,
    full_name: str | None = None,
    tenant_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
) -> bool:
    """Email the verification link. Returns ``True`` if SMTP delivered it.

    On ``False`` (SMTP unconfigured / send failed) the URL is logged at
    INFO so a small/dev deployment can still complete the flow manually.
    """
    greeting = f"Hi {full_name}," if full_name else "Hi,"
    body = (
        f"{greeting}\n\n"
        "Thanks for creating a Daalu account. Confirm your email address to "
        "activate it:\n\n"
        f"{verify_url}\n\n"
        "If you didn't request this, you can safely ignore this email.\n\n"
        "— The Daalu team"
    )
    delivered = await notify.send_email(
        to=to_email,
        subject="Confirm your Daalu account",
        body_markdown=body,
        tenant_id=tenant_id,
        db=db,
    )
    if delivered:
        logger.info("signup.verification_delivered", to=to_email)
    else:
        # Dev / unconfigured-SMTP fallback: surface the link so the flow
        # can still be completed manually.
        logger.info("signup.verification_link", to=to_email, url=verify_url)
    return delivered
