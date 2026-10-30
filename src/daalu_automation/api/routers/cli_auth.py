"""`daalu login` — device-authorization endpoints.

Two flows, one table (``cli_device_authorizations``), one redeem path:

* **device flow** — the CLI starts an authorization (``/device/code``), the user
  approves it in the browser (``/device/approve`` from the ``/cli/activate``
  page), and the CLI polls (``/device/token``) until it can redeem a PAT.
* **express flow** — the workspace page mints a pre-approved row
  (``/login-command``) and hands the user a ``daalu login --code …`` command; the
  CLI redeems it directly, no browser step.

Auth split: ``/device/code`` and ``/device/token`` are **public** (the CLI has no
session yet; the device_code is itself the secret) and are listed in
``settings.auth_public_paths``. Everything else is ``current_user`` — those are
hit by the already-logged-in browser.

The redeemed credential is a normal ``dpat_`` personal-access-token, so it shows
up in /settings → API tokens and is revocable like any other.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_user
from daalu_automation.config import get_settings
from daalu_automation.core.auth import generate_pat, hash_pat
from daalu_automation.core.cli_auth import (
    generate_device_code,
    generate_user_code,
    hash_device_code,
    normalize_user_code,
)
from daalu_automation.database import get_db
from daalu_automation.models.cli_device import (
    DEVICE_APPROVED,
    DEVICE_DENIED,
    DEVICE_PENDING,
    DEVICE_REDEEMED,
    CliDeviceAuthorization,
)
from daalu_automation.models.personal_access_token import PersonalAccessToken
from daalu_automation.models.user import User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/cli", tags=["cli"])

# How long a device authorization stays redeemable, and how often the CLI
# should poll. Short — the user is sitting at the terminal.
_DEVICE_TTL_SECONDS = 600
_POLL_INTERVAL_SECONDS = 5


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ── schemas ───────────────────────────────────────────────────────────────


class DeviceCodeRequest(BaseModel):
    # Free-text label the CLI sends (hostname etc.). Becomes the PAT name so
    # the user can tell their laptops apart in /settings → API tokens.
    client_name: str | None = Field(default=None, max_length=128)


class DeviceCodeResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    interval: int
    expires_in: int


class DeviceTokenRequest(BaseModel):
    device_code: str = Field(max_length=128)


class DeviceTokenResponse(BaseModel):
    # Discriminator the CLI branches on:
    #   "authorization_pending" | "approved" | "denied" | "expired"
    status: str
    # Present only when status == "approved".
    access_token: str | None = None
    token_type: str | None = None
    host: str | None = None


class DevicePendingView(BaseModel):
    user_code: str
    client_name: str | None
    status: str
    expires_at: datetime


class UserCodeRequest(BaseModel):
    user_code: str = Field(max_length=16)


class LoginCommandResponse(BaseModel):
    command: str
    device_code: str
    expires_in: int


# ── helpers ───────────────────────────────────────────────────────────────


def _verification_uri() -> tuple[str, str]:
    """(base_uri, path) for the browser approval page."""
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/cli/activate", base


async def _mint_pat_for(
    db: AsyncSession, *, user: User, name: str
) -> tuple[str, PersonalAccessToken]:
    """Create a PAT row for ``user`` and return ``(cleartext, row)``.

    The cleartext is shown to the CLI exactly once; only its hash persists.
    ``flush`` populates ``pat.id`` without ending the caller's transaction, so
    the device row can reference it in the same commit.
    """
    cleartext = generate_pat()
    pat = PersonalAccessToken(
        user_id=user.id,
        tenant_id=user.tenant_id,
        name=name[:128],
        token_hash=hash_pat(cleartext),
        prefix=cleartext[:12],
    )
    db.add(pat)
    await db.flush()
    return cleartext, pat


# ── public endpoints (no session) ─────────────────────────────────────────


@router.post("/device/code", response_model=DeviceCodeResponse)
async def start_device_flow(
    payload: DeviceCodeRequest,
    db: AsyncSession = Depends(get_db),
) -> DeviceCodeResponse:
    """Begin a device authorization. Returns the codes the CLI needs.

    Public — the CLI has no session yet. The ``device_code`` is the secret
    the CLI will redeem with; only its hash is stored.
    """
    device_code = generate_device_code()
    # user_code must be unique; collisions are astronomically unlikely but
    # retry a couple of times to be safe.
    for _ in range(5):
        user_code = generate_user_code()
        exists = (
            await db.execute(
                select(CliDeviceAuthorization.id).where(
                    CliDeviceAuthorization.user_code == user_code
                )
            )
        ).first()
        if not exists:
            break
    else:  # pragma: no cover — five collisions is essentially impossible
        raise HTTPException(500, "could not allocate a user code")

    row = CliDeviceAuthorization(
        device_code_hash=hash_device_code(device_code),
        user_code=user_code,
        status=DEVICE_PENDING,
        client_name=(payload.client_name or "").strip() or None,
        expires_at=_now() + timedelta(seconds=_DEVICE_TTL_SECONDS),
    )
    db.add(row)
    await db.commit()

    verify_uri, _ = _verification_uri()
    return DeviceCodeResponse(
        device_code=device_code,
        user_code=user_code,
        verification_uri=verify_uri,
        verification_uri_complete=f"{verify_uri}?code={user_code}",
        interval=_POLL_INTERVAL_SECONDS,
        expires_in=_DEVICE_TTL_SECONDS,
    )


@router.post("/device/token", response_model=DeviceTokenResponse)
async def redeem_device_token(
    payload: DeviceTokenRequest,
    db: AsyncSession = Depends(get_db),
) -> DeviceTokenResponse:
    """Poll/redeem. Public — the device_code authenticates the call itself.

    Always returns 200 with a ``status`` discriminator so the CLI's poll loop
    is a simple JSON branch rather than HTTP-status guessing.
    """
    row = (
        await db.execute(
            select(CliDeviceAuthorization).where(
                CliDeviceAuthorization.device_code_hash
                == hash_device_code(payload.device_code)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        # Unknown code — treat as denied rather than leaking existence.
        return DeviceTokenResponse(status=DEVICE_DENIED)

    if row.status == DEVICE_REDEEMED:
        # Already used — never mint twice for one code.
        return DeviceTokenResponse(status=DEVICE_DENIED)
    if row.status == DEVICE_DENIED:
        return DeviceTokenResponse(status=DEVICE_DENIED)
    if row.expires_at < _now():
        return DeviceTokenResponse(status="expired")
    if row.status == DEVICE_PENDING:
        return DeviceTokenResponse(status="authorization_pending")

    # status == approved → mint the PAT under this transaction and flip the
    # row to redeemed so a replay of the same device_code can't get a second.
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        return DeviceTokenResponse(status=DEVICE_DENIED)
    cleartext, pat = await _mint_pat_for(
        db, user=user, name=row.client_name or "Daalu CLI"
    )
    row.status = DEVICE_REDEEMED
    row.redeemed_at = _now()
    row.pat_id = pat.id
    await db.commit()
    logger.info("cli.device_redeemed", user_id=str(user.id), pat_id=str(pat.id))
    return DeviceTokenResponse(
        status="approved",
        access_token=cleartext,
        token_type="pat",
        host=get_settings().public_base_url.rstrip("/") or None,
    )


# ── browser endpoints (cookie session) ─────────────────────────────────────


@router.get("/device/pending", response_model=DevicePendingView)
async def device_pending(
    user_code: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> DevicePendingView:
    """What the ``/cli/activate`` page shows before the user approves."""
    row = await _lookup_user_code(db, user_code)
    return DevicePendingView(
        user_code=row.user_code,
        client_name=row.client_name,
        status=row.status,
        expires_at=row.expires_at,
    )


@router.post("/device/approve", response_model=DevicePendingView)
async def device_approve(
    payload: UserCodeRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> DevicePendingView:
    """Bind a pending device authorization to the logged-in user."""
    row = await _lookup_user_code(db, payload.user_code)
    if row.expires_at < _now():
        raise HTTPException(400, "this login request has expired — run `daalu login` again")
    if row.status not in (DEVICE_PENDING, DEVICE_APPROVED):
        raise HTTPException(409, f"request already {row.status}")
    row.status = DEVICE_APPROVED
    row.tenant_id = user.tenant_id
    row.user_id = user.id
    row.approved_at = _now()
    await db.commit()
    logger.info("cli.device_approved", user_id=str(user.id), user_code=row.user_code)
    return DevicePendingView(
        user_code=row.user_code,
        client_name=row.client_name,
        status=row.status,
        expires_at=row.expires_at,
    )


@router.post("/device/deny", status_code=204)
async def device_deny(
    payload: UserCodeRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    """Reject a device authorization (wrong machine, didn't initiate, etc.)."""
    row = await _lookup_user_code(db, payload.user_code)
    if row.status not in (DEVICE_REDEEMED,):
        row.status = DEVICE_DENIED
        await db.commit()


@router.post("/login-command", response_model=LoginCommandResponse)
async def login_command(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> LoginCommandResponse:
    """Mint a pre-approved device authorization for the express paste flow.

    The returned ``device_code`` is embedded in a ``daalu login --code …``
    command the workspace page shows. The CLI redeems it directly (no browser
    round-trip), since the row is created already approved for this user.
    """
    device_code = generate_device_code()
    # user_code is irrelevant for the express flow but the column is unique +
    # non-null, so allocate a throwaway one.
    user_code = generate_user_code()
    row = CliDeviceAuthorization(
        device_code_hash=hash_device_code(device_code),
        user_code=user_code,
        status=DEVICE_APPROVED,
        tenant_id=user.tenant_id,
        user_id=user.id,
        approved_at=_now(),
        client_name="Daalu CLI",
        expires_at=_now() + timedelta(seconds=_DEVICE_TTL_SECONDS),
    )
    db.add(row)
    await db.commit()
    # Embed --host so the command targets *this* operator instance; the redeem
    # call (/cli/device/token) has to land on the same host that minted it.
    base = get_settings().public_base_url.rstrip("/")
    host_flag = f"--host {base} " if base else ""
    return LoginCommandResponse(
        command=f"daalu login {host_flag}--code {device_code}",
        device_code=device_code,
        expires_in=_DEVICE_TTL_SECONDS,
    )


async def _lookup_user_code(
    db: AsyncSession, user_code: str
) -> CliDeviceAuthorization:
    row = (
        await db.execute(
            select(CliDeviceAuthorization).where(
                CliDeviceAuthorization.user_code == normalize_user_code(user_code)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "no such login request")
    return row
