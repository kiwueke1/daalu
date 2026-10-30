"""Shared FastAPI dependencies.

Tenancy model. Every authenticated request resolves a ``tenant_id`` from
the user's JWT — the ``tid`` claim is the only trusted source. There is
**no header-based tenant override** — Phase-1 had ``X-Tenant-ID`` and it
was removed because any user with a valid JWT could read any tenant's
rows just by spoofing the header.

The webhook ingest path (``POST /api/v1/events``) is the one exception:
it has no session cookie, so the per-tenant ``X-Daalu-Key`` ingest key
serves double duty as both auth and tenant resolution — the hash is
looked up against ``tenants.ingest_api_key_hash``.

DEFAULT_TENANT_ID remains the bootstrap tenant — see ``core/bootstrap.py``
— so a fresh install with one user keeps working without an explicit
``POST /tenants`` step.
"""

from __future__ import annotations

import uuid

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import DEFAULT_TENANT_ID  # noqa: F401 — re-exported for tests
from daalu_automation.core.auth import (
    TokenError,
    decode_token,
    hash_ingest_api_key,
    hash_pat,
    looks_like_pat,
)
from daalu_automation.database import get_db
from daalu_automation.models import PersonalAccessToken, Tenant, User


async def current_user(
    daalu_session: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the authenticated user from cookie, JWT, or PAT.

    Cookie is the default path for the browser UI; ``Authorization:
    Bearer`` covers both the JWT issued at login *and* personal access
    tokens (prefixed ``dpat_``) minted from /settings → API tokens.
    PATs are looked up by their sha256 hash and update ``last_used_at``
    so the UI can show "last used 3 days ago".
    """
    token = daalu_session
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(None, 1)[1]
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # PAT path — cheap prefix check, then a hash lookup. Skips JWT decode.
    if looks_like_pat(token):
        return await _resolve_pat(token, db)

    try:
        payload = decode_token(token)
    except TokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as e:
        raise HTTPException(401, "invalid token subject") from e
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "user not found or inactive")
    return user


async def _resolve_pat(token: str, db: AsyncSession) -> User:
    """Look up a PAT by hash, gate on expiry/revocation, return the user.

    Also stamps ``last_used_at`` so the UI can sort by recency. The
    update is a fire-and-forget — failure to record it must not break
    the request.
    """
    from datetime import datetime, timezone

    digest = hash_pat(token)
    stmt = select(PersonalAccessToken).where(PersonalAccessToken.token_hash == digest)
    pat = (await db.execute(stmt)).scalar_one_or_none()
    if pat is None:
        raise HTTPException(401, "invalid token")
    now = datetime.now(tz=timezone.utc)
    if pat.revoked_at is not None:
        raise HTTPException(401, "token revoked")
    if pat.expires_at is not None and pat.expires_at < now:
        raise HTTPException(401, "token expired")
    user = await db.get(User, pat.user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "user not found or inactive")
    try:
        pat.last_used_at = now
        await db.commit()
    except Exception:  # pragma: no cover — best-effort tracker
        await db.rollback()
    return user


async def current_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "admin privileges required")
    return user


async def current_superuser(user: User = Depends(current_user)) -> User:
    """Gate for platform-level operations (tenant management).

    Distinct from ``current_admin``: a tenant-admin can manage users
    within their tenant but cannot create or list other tenants. Only
    superusers can hit ``/api/v1/tenants/*``.
    """
    if not user.is_superuser:
        raise HTTPException(403, "superuser privileges required")
    return user


async def current_tenant_id(
    request: Request,
    user: User = Depends(current_user),
) -> uuid.UUID:
    """Resolve the current tenant from the authenticated user.

    Tenant is taken from ``user.tenant_id`` (the JWT's ``tid`` claim is
    verified by ``current_user`` matching the same user row). Any
    ``X-Tenant-ID`` header is **ignored** — see module docstring for
    why. For the webhook ingest path, see ``verify_ingest_key`` which
    sets ``request.state.tenant_id`` directly.
    """
    # Webhook ingest may have already resolved a tenant from the key.
    state_tenant = getattr(request.state, "tenant_id", None)
    if state_tenant is not None:
        return state_tenant
    return user.tenant_id


async def verify_ingest_key(
    request: Request,
    x_daalu_key: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> uuid.UUID:
    """Authenticate a webhook POST + resolve which tenant it speaks for.

    The cleartext key from ``X-Daalu-Key`` is sha256-hashed and looked up
    against ``tenants.ingest_api_key_hash``. The resolved tenant is
    stashed on ``request.state.tenant_id`` so the route handler — and
    the chained ``current_tenant_id`` dep — uses it instead of falling
    back to the authenticated user (there is none on this path).

    Returns the resolved ``tenant_id`` so the route can use it directly.
    Rejects with 401 if the key is missing, malformed, or doesn't match
    any tenant. **Never** falls open — Phase-1's "if INGEST_API_KEY is
    empty the gate is open" was removed because a misconfigured deploy
    would silently accept anonymous events for the default tenant.
    """
    if not x_daalu_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Daalu-Key",
        )
    key_hash = hash_ingest_api_key(x_daalu_key)
    stmt = select(Tenant).where(
        Tenant.ingest_api_key_hash == key_hash,
        Tenant.is_deleted.is_(False),
    )
    tenant = (await db.execute(stmt)).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid X-Daalu-Key",
        )
    request.state.tenant_id = tenant.id
    return tenant.id
