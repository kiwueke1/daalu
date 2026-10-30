"""Team invites — admin-only CRUD spec'd in book-customer §6.

Endpoints:

  POST    /invites          mint + email an invite          (admin)
  GET     /invites          list active invites for tenant  (admin)
  POST    /invites/{id}/resend  re-issue token, re-deliver  (admin)
  DELETE  /invites/{id}     revoke (cannot be undone)       (admin)

Redemption lives in :mod:`api.routers.auth` (``POST /auth/redeem-invite``)
because it's an unauthenticated path — the token IS the auth.

Cleartext invite tokens are returned to the inviter once at create /
resend time and never again. ``token_hash`` is the only persisted
form.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_admin
from daalu_automation.core.invites import (
    build_invite_url,
    generate_invite_token,
    hash_invite_token,
    send_invite_email,
)
from daalu_automation.database import get_db
from daalu_automation.models import Invite, Tenant, User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/invites", tags=["invites"])

_DEFAULT_TTL_DAYS = 7


# ── Schemas ──────────────────────────────────────────────────────────────


class InviteCreate(BaseModel):
    email: EmailStr
    role: Literal["admin", "user"] = "user"
    message: str | None = Field(default=None, max_length=2000)


class InviteView(BaseModel):
    """Invite as seen by the inviting admin.

    Cleartext ``token`` is included ONLY on create/resend responses
    (where it is shown once). All other reads omit it.
    """

    id: str
    email: str
    role: str
    status: Literal["pending", "expired", "revoked", "accepted"]
    invited_by_user_id: str | None
    inviter_name: str | None = None
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    # Only populated on POST /invites or POST /invites/{id}/resend.
    token: str | None = None
    invite_url: str | None = None
    # True iff the invite was actually delivered via SMTP. When False
    # the inviter UI shows the "copy link manually" banner so the
    # invitee still gets in.
    delivered: bool | None = None


def _classify(inv: Invite, now: datetime) -> str:
    if inv.accepted_at is not None:
        return "accepted"
    if inv.revoked_at is not None:
        return "revoked"
    if inv.expires_at <= now:
        return "expired"
    return "pending"


def _to_view(
    inv: Invite,
    *,
    now: datetime,
    inviter_name: str | None,
    token: str | None = None,
    delivered: bool | None = None,
) -> InviteView:
    return InviteView(
        id=str(inv.id),
        email=inv.email,
        role=inv.role,
        status=_classify(inv, now),  # type: ignore[arg-type]
        invited_by_user_id=str(inv.invited_by_user_id) if inv.invited_by_user_id else None,
        inviter_name=inviter_name,
        expires_at=inv.expires_at,
        accepted_at=inv.accepted_at,
        revoked_at=inv.revoked_at,
        created_at=inv.created_at,
        token=token,
        invite_url=build_invite_url(token) if token else None,
        delivered=delivered,
    )


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("", response_model=InviteView, status_code=201)
async def create_invite(
    body: InviteCreate,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
) -> InviteView:
    """Mint a fresh invite and trigger delivery.

    Refuses if the email already has a *pending* invite on this
    tenant (use Resend instead). Also refuses if the email already
    belongs to an active user on this tenant — the operator should
    talk to the existing user, not invite them again.
    """
    email = body.email.lower().strip()
    now = datetime.now(tz=timezone.utc)

    existing_user = (
        await db.execute(
            select(User).where(User.tenant_id == admin.tenant_id, User.email == email)
        )
    ).scalar_one_or_none()
    if existing_user is not None and existing_user.is_active:
        raise HTTPException(409, "an active user with this email already exists in your tenant")

    pending = (
        await db.execute(
            select(Invite).where(
                Invite.tenant_id == admin.tenant_id,
                Invite.email == email,
                Invite.accepted_at.is_(None),
                Invite.revoked_at.is_(None),
                Invite.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    if pending is not None:
        raise HTTPException(
            409,
            "this email already has a pending invite — use Resend instead",
        )

    cleartext = generate_invite_token()
    inv = Invite(
        tenant_id=admin.tenant_id,
        email=email,
        role=body.role,
        token_hash=hash_invite_token(cleartext),
        invited_by_user_id=admin.id,
        message=body.message,
        expires_at=now + timedelta(days=_DEFAULT_TTL_DAYS),
    )
    db.add(inv)
    await db.commit()
    await db.refresh(inv)

    tenant = await db.get(Tenant, admin.tenant_id)
    delivered = await send_invite_email(
        to_email=email,
        invite_url=build_invite_url(cleartext),
        inviter_name=admin.full_name or admin.email,
        tenant_name=tenant.name if tenant else "",
        role=body.role,
        message=body.message,
        tenant_id=admin.tenant_id,
        db=db,
    )
    return _to_view(
        inv,
        now=now,
        inviter_name=admin.full_name or admin.email,
        token=cleartext,
        delivered=delivered,
    )


@router.get("", response_model=list[InviteView])
async def list_invites(
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
    include_terminal: bool = False,
) -> list[InviteView]:
    """List invites on the caller's tenant.

    By default returns only pending + expired (i.e. anything still
    actionable). Pass ``include_terminal=true`` to include accepted
    and revoked rows in the response — useful for audit views.
    """
    now = datetime.now(tz=timezone.utc)
    stmt = select(Invite).where(Invite.tenant_id == admin.tenant_id).order_by(
        Invite.created_at.desc()
    )
    rows = (await db.execute(stmt)).scalars().all()
    # Resolve inviter names in one pass to avoid N+1.
    inviter_ids = {r.invited_by_user_id for r in rows if r.invited_by_user_id}
    inviters: dict[str, str] = {}
    if inviter_ids:
        users = (
            await db.execute(select(User).where(User.id.in_(inviter_ids)))
        ).scalars().all()
        for u in users:
            inviters[str(u.id)] = u.full_name or u.email
    views: list[InviteView] = []
    for r in rows:
        status = _classify(r, now)
        if not include_terminal and status in ("accepted", "revoked"):
            continue
        views.append(
            _to_view(
                r,
                now=now,
                inviter_name=inviters.get(str(r.invited_by_user_id) if r.invited_by_user_id else ""),
            )
        )
    return views


@router.post("/{invite_id}/resend", response_model=InviteView)
async def resend_invite(
    invite_id: str,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
) -> InviteView:
    """Issue a fresh token + email for an existing pending invite.

    The old token is invalidated (its hash is overwritten). Accepted
    or revoked invites cannot be resent — create a new invite instead.
    """
    inv = await db.get(Invite, invite_id)
    if inv is None or inv.tenant_id != admin.tenant_id:
        raise HTTPException(404, "invite not found")
    if inv.accepted_at is not None or inv.revoked_at is not None:
        raise HTTPException(409, "invite is no longer pending")

    now = datetime.now(tz=timezone.utc)
    cleartext = generate_invite_token()
    inv.token_hash = hash_invite_token(cleartext)
    inv.expires_at = now + timedelta(days=_DEFAULT_TTL_DAYS)
    inv.updated_at = now
    await db.commit()

    tenant = await db.get(Tenant, admin.tenant_id)
    delivered = await send_invite_email(
        to_email=inv.email,
        invite_url=build_invite_url(cleartext),
        inviter_name=admin.full_name or admin.email,
        tenant_name=tenant.name if tenant else "",
        role=inv.role,
        message=inv.message,
        tenant_id=admin.tenant_id,
        db=db,
    )
    return _to_view(
        inv,
        now=now,
        inviter_name=admin.full_name or admin.email,
        token=cleartext,
        delivered=delivered,
    )


@router.delete("/{invite_id}", status_code=204)
async def revoke_invite(
    invite_id: str,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a pending invite. Idempotent — already-revoked is a 204.

    Accepted invites cannot be revoked (the user already exists; use
    the user-deactivation flow instead).
    """
    inv = await db.get(Invite, invite_id)
    if inv is None or inv.tenant_id != admin.tenant_id:
        raise HTTPException(404, "invite not found")
    if inv.accepted_at is not None:
        raise HTTPException(409, "cannot revoke an accepted invite — deactivate the user instead")
    if inv.revoked_at is None:
        inv.revoked_at = datetime.now(tz=timezone.utc)
        await db.commit()
    return None
