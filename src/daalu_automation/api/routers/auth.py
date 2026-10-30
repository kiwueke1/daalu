"""Login / logout / current-user / profile / personal-access tokens.

The browser UI uses the cookie path: ``POST /auth/login`` sets the
``daalu_session`` HttpOnly cookie. ``GET /auth/me`` is the page-load
boot probe — frontend redirects to ``/login`` on 401. ``POST /auth/logout``
clears the cookie.

Scripts and curl callers can skip the cookie entirely — the ``token``
field in the login response is the same JWT, usable as
``Authorization: Bearer <token>``. Long-lived automation should mint a
personal access token via ``POST /auth/tokens`` instead — JWTs expire
quickly, PATs only when revoked or explicitly given an ``expires_at``.

The settings page also uses this router for profile edits
(``PATCH /auth/me``), password changes (``POST /auth/password``), and
PAT management (``GET /auth/tokens`` / ``POST /auth/tokens`` /
``DELETE /auth/tokens/{id}``).
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field  # noqa: F401 — EmailStr requires email-validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_user
from daalu_automation.config import get_settings
from daalu_automation.core import oidc
from daalu_automation.core.auth import (
    TokenError,
    decode_token,
    generate_pat,
    hash_password,
    hash_pat,
    issue_token,
    verify_password,
)
from daalu_automation.database import get_db
from daalu_automation.models import PersonalAccessToken, User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    token: str
    expires_at: datetime
    user: MeResponse


class MeResponse(BaseModel):
    id: str
    email: str
    full_name: str | None
    is_admin: bool
    # Platform-level operator (manages tenants; gates site-wide tools like the
    # AI-factory AIPerf benchmarking panel). Distinct from is_admin (tenant
    # admin). See api/deps.current_superuser.
    is_superuser: bool = False
    tenant_id: str
    preferences: dict[str, Any] = Field(default_factory=dict)


class UpdateMeRequest(BaseModel):
    full_name: str | None = None
    preferences: dict[str, Any] | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=256)


class PatListItem(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class PatCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # Optional days-from-now expiry. None → never expires.
    expires_in_days: int | None = Field(default=None, ge=1, le=365 * 5)


class PatCreateResponse(PatListItem):
    # Cleartext token, returned exactly once. The client must store it
    # immediately — the server only keeps the hash.
    token: str


class InvitePreview(BaseModel):
    """Public-readable snapshot of an invite for the accept-invite page.

    Returned by ``GET /auth/invite/{token}`` so the front-end can
    render "You're invited to <tenant> as <role>" before the user
    submits their name + password. Contains nothing the invitee
    couldn't already see by being the holder of the token, but we
    deliberately exclude the inviter's email to keep the surface
    minimal.
    """

    email: str
    role: str
    tenant_name: str
    inviter_name: str | None
    message: str | None
    expires_at: datetime


class RedeemInviteRequest(BaseModel):
    token: str
    # Optional display name — the email comes from the invite row,
    # immutable so the inviter knows exactly who's signing up.
    full_name: str | None = Field(default=None, max_length=255)
    password: str = Field(min_length=8, max_length=256)


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    full_name: str | None = Field(default=None, max_length=255)
    # Optional org/team name — becomes the new tenant's display name.
    organization: str | None = Field(default=None, max_length=255)


class SignupResponse(BaseModel):
    # Deliberately neutral — same body whether or not the email was already
    # registered, so signup can't be used to enumerate accounts.
    status: str = "verification_sent"


class VerifyEmailRequest(BaseModel):
    token: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


LoginResponse.model_rebuild()


def _set_session_cookie(response: Response, token: str) -> None:
    """Set the ``daalu_session`` cookie exactly as /login does.

    Shared by /login, /redeem-invite, and the OIDC callback so the cookie
    attributes (HttpOnly/Secure/SameSite/Max-Age) never drift between
    login paths.
    """
    settings = get_settings()
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
        # Parent-domain cookie (e.g. ``.daalu.io``) so the NV-CM tool proxy
        # on ``*.cmtools.daalu.io`` sees the session. Empty → host-only.
        domain=settings.auth_cookie_domain or None,
    )


def _safe_next(next_url: str | None) -> str:
    """Constrain a post-login redirect to a local path (open-redirect guard).

    Only same-site absolute paths are honoured; anything with a scheme/host
    or protocol-relative (``//evil``) falls back to the app root.
    """
    if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url


def _to_me(user: User) -> MeResponse:
    return MeResponse(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        is_admin=user.is_admin,
        is_superuser=user.is_superuser,
        tenant_id=str(user.tenant_id),
        preferences=user.preferences or {},
    )


def _to_pat_item(pat: PersonalAccessToken) -> PatListItem:
    return PatListItem(
        id=str(pat.id),
        name=pat.name,
        prefix=pat.prefix,
        created_at=pat.created_at,
        last_used_at=pat.last_used_at,
        expires_at=pat.expires_at,
        revoked_at=pat.revoked_at,
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    # When the hub is in SSO mode, local password login is normally disabled so
    # there is a single identity provider (Keycloak) — but it is re-enabled when
    # EITHER self-service signup (``self_signup_enabled``) OR operator-provisioned
    # native login (``native_password_login_enabled``) is on, so local
    # email+password accounts (seeded admins, accepted invites) can sign in
    # alongside Google SSO. SSO-only users still can't use this path (they have
    # no ``hashed_password`` — the check below 401s them).
    settings = get_settings()
    native_login_ok = (
        settings.self_signup_enabled or settings.native_password_login_enabled
    )
    if settings.auth_mode == "oidc" and not native_login_ok:
        raise HTTPException(
            403, "password login is disabled; sign in via SSO (/api/v1/auth/oidc/login)"
        )
    stmt = select(User).where(User.email == payload.email.lower())
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or not user.hashed_password:
        raise HTTPException(401, "invalid email or password")
    if not user.is_active:
        raise HTTPException(403, "user is inactive")
    if not verify_password(payload.password, user.hashed_password):
        raise HTTPException(401, "invalid email or password")

    token, expires_at = issue_token(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        is_admin=user.is_admin,
    )
    _set_session_cookie(response, token)
    return LoginResponse(token=token, expires_at=expires_at, user=_to_me(user))


class SSOProvider(BaseModel):
    # Keycloak IdP alias passed as ``kc_idp_hint`` (e.g. "google").
    id: str
    # Human label for the button, e.g. "Google".
    label: str


class AuthConfigResponse(BaseModel):
    # "password" → local email+password form; "oidc" → Daalu-branded SSO buttons.
    mode: str
    # Convenience: where the frontend should send users to start SSO.
    sso_login_path: str
    # In "oidc" mode, the identity providers Daalu can broker on the user's
    # behalf. The login page renders one button per provider that deep-links to
    # ``{sso_login_path}?idp={id}`` so the user goes straight to that provider
    # (Google) without ever seeing Keycloak's own login screen.
    sso_providers: list[SSOProvider] = []
    # Whether visitors may create a local account (``POST /auth/signup``). Drives
    # the "Create an account" link on the login page.
    self_signup_enabled: bool = False
    # Whether the local email+password form should be shown. True in "password"
    # mode, and also in "oidc" mode when self-signup is on (so local accounts can
    # log in next to the SSO buttons).
    password_login_enabled: bool = True


@router.get("/config", response_model=AuthConfigResponse)
async def auth_config():
    """Public: tells the frontend which login mode the hub is running.

    Unauthenticated by design (it's the login surface) — reveals only the
    mode, no secrets. The login page uses it to either render the password
    form or bounce straight to ``/auth/oidc/login``.
    """
    settings = get_settings()
    providers: list[SSOProvider] = []
    # Advertise Google when the realm has Google wired. We check for *either*
    # credential because the IdP can be provisioned out-of-band (Keycloak holds
    # the clientId) while only the secret rides Daalu's env — what matters is
    # that Google SSO is configured for this deployment. Google is brokered
    # through Keycloak, but the user only ever sees Daalu's button → Google;
    # "google" is the Keycloak IdP alias passed as kc_idp_hint.
    google_configured = bool(
        settings.keycloak_google_client_id or settings.keycloak_google_client_secret
    )
    if settings.auth_mode == "oidc" and google_configured:
        providers.append(SSOProvider(id="google", label="Google"))
    return AuthConfigResponse(
        mode=settings.auth_mode,
        sso_login_path=f"{settings.api_v1_prefix}/auth/oidc/login",
        sso_providers=providers,
        self_signup_enabled=settings.self_signup_enabled,
        # Local form shows in password mode, or in oidc mode once native login
        # is enabled (by self-signup OR operator-provisioned native login).
        password_login_enabled=(
            settings.auth_mode == "password"
            or settings.self_signup_enabled
            or settings.native_password_login_enabled
        ),
    )


@router.post("/logout", status_code=204)
async def logout(response: Response):
    settings = get_settings()
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path="/",
        domain=settings.auth_cookie_domain or None,
    )
    return Response(status_code=204)


# ── Keycloak hub SSO (OIDC authorization-code + PKCE) ──────────────────
#
# These three routes implement the *human* login to ops.daalu.io against
# the same Keycloak realm the NV-CM tools trust, so the browser already
# holds a Keycloak session and the deep links into Nautobot / NV-CM UIs
# need no second prompt. Active when ``auth_mode == "oidc"``; the frontend
# sends users here instead of the password form. Identity stays
# daalu-owned: the verified ``email`` claim maps to a ``User`` row and we
# issue the same ``daalu_session`` cookie everything else already trusts.
#
# The short-lived state/nonce/PKCE values ride HttpOnly cookies between
# /login and /callback (SameSite=Lax is correct — the callback is a
# top-level GET navigation back from Keycloak).

_OIDC_TXN_MAX_AGE = 600  # seconds; the login round-trip is short


def _callback_url(request: Request) -> str:
    """Absolute redirect URI for the OIDC callback.

    Prefers the explicit setting; otherwise derives the public origin from
    the proxy's forwarded headers (the ops.daalu.io ingress terminates TLS
    and sets X-Forwarded-Proto/Host).
    """
    settings = get_settings()
    if settings.keycloak_hub_redirect_url:
        return settings.keycloak_hub_redirect_url
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{scheme}://{host}{settings.api_v1_prefix}/auth/oidc/callback"


def _set_txn_cookie(response: Response, name: str, value: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=name,
        value=value,
        max_age=_OIDC_TXN_MAX_AGE,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.get("/oidc/login")
async def oidc_login(request: Request, next: str | None = None, idp: str | None = None):
    """Begin the OIDC login: redirect the browser to Keycloak.

    ``idp`` (e.g. ``"google"``) is forwarded as ``kc_idp_hint`` so Keycloak
    skips its own login page and bounces straight to that provider — the user
    only ever sees Daalu's login screen and then Google. Omitting ``idp`` lands
    on Keycloak's chooser (kept as a fallback, not the primary path).
    """
    if get_settings().auth_mode != "oidc":
        raise HTTPException(404, "OIDC login is not enabled")
    state = oidc.new_state()
    nonce = oidc.new_nonce()
    verifier = oidc.new_code_verifier()
    redirect_uri = _callback_url(request)
    try:
        authorize_url = await oidc.build_authorize_url(
            redirect_uri=redirect_uri,
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            idp_hint=idp or None,
        )
    except oidc.OIDCError as e:
        raise HTTPException(503, f"SSO is misconfigured: {e}") from e
    resp = RedirectResponse(authorize_url, status_code=302)
    _set_txn_cookie(resp, "oidc_state", state)
    _set_txn_cookie(resp, "oidc_nonce", nonce)
    _set_txn_cookie(resp, "oidc_verifier", verifier)
    _set_txn_cookie(resp, "oidc_next", _safe_next(next))
    return resp


@router.get("/oidc/callback")
async def oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    oidc_state: str | None = Cookie(default=None),
    oidc_nonce: str | None = Cookie(default=None),
    oidc_verifier: str | None = Cookie(default=None),
    oidc_next: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Complete the OIDC login: validate, map to a User, set the session."""
    if get_settings().auth_mode != "oidc":
        raise HTTPException(404, "OIDC login is not enabled")
    if error:
        raise HTTPException(400, f"SSO error: {error}")
    if not code or not state:
        raise HTTPException(400, "missing code/state")
    if not oidc_state or state != oidc_state:
        raise HTTPException(400, "OIDC state mismatch (expired or forged)")
    if not oidc_verifier or not oidc_nonce:
        raise HTTPException(400, "OIDC transaction expired; restart sign-in")

    redirect_uri = _callback_url(request)
    try:
        tokens = await oidc.exchange_code(
            code=code, redirect_uri=redirect_uri, code_verifier=oidc_verifier
        )
        id_token = tokens.get("id_token")
        if not id_token:
            raise oidc.OIDCError("token response had no id_token")
        claims = await oidc.validate_id_token(id_token, nonce=oidc_nonce)
    except oidc.OIDCError as e:
        raise HTTPException(401, f"SSO sign-in failed: {e}") from e

    email = (claims.get("email") or "").lower()
    if not email:
        raise HTTPException(401, "SSO token has no email claim")

    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()

    settings = get_settings()
    if user is None and settings.oidc_auto_provision_users:
        from daalu_automation.config import DEFAULT_TENANT_ID

        user = User(
            email=email,
            full_name=claims.get("name"),
            hashed_password=None,  # SSO-only account; no local password
            is_admin=False,
            is_active=True,
            tenant_id=DEFAULT_TENANT_ID,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    if user is None:
        raise HTTPException(403, f"no daalu account for {email}; ask an admin to invite you")
    if not user.is_active:
        raise HTTPException(403, "user is inactive")

    # Carry the user's Keycloak refresh token (encrypted) in the session so the
    # NV-CM tool proxy can token-exchange it into a per-user downstream token and
    # log the user into the tools as themselves (see api/tool_proxy.py). Login
    # requests ``offline_access`` (see core/oidc.build_authorize_url), so this is
    # an OFFLINE refresh token that survives the realm's 30-minute SSO-session
    # idle timeout — an online token would go inactive mid-session and drop the
    # user to the tool's own login.
    extra: dict = {}
    refresh_token = tokens.get("refresh_token")
    if refresh_token:
        from daalu_automation.core.crypto import encrypt_secret

        extra["krt"] = encrypt_secret(refresh_token)
    # Keep the id_token for a clean RP-initiated logout (id_token_hint) so
    # Keycloak skips its "confirm logout" interstitial.
    if id_token:
        extra["kit"] = id_token
    token, _expires = issue_token(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        is_admin=user.is_admin,
        extra=extra or None,
    )
    resp = RedirectResponse(_safe_next(oidc_next), status_code=302)
    _set_session_cookie(resp, token)
    for c in ("oidc_state", "oidc_nonce", "oidc_verifier", "oidc_next"):
        resp.delete_cookie(c, path="/")
    return resp


@router.get("/oidc/logout")
async def oidc_logout(request: Request):
    """Clear the daalu session and end the Keycloak SSO session (RP-initiated)."""
    settings = get_settings()
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    post_logout = settings.keycloak_hub_post_logout_url or f"{scheme}://{host}/login"
    target = post_logout
    # Pass the user's id_token as id_token_hint so Keycloak skips its
    # "confirm logout" interstitial and redirects straight back.
    id_token_hint: str | None = None
    sess = request.cookies.get(settings.auth_cookie_name)
    if sess:
        try:
            id_token_hint = decode_token(sess).get("kit")
        except TokenError:
            pass
    if settings.auth_mode == "oidc":
        try:
            url = await oidc.end_session_url(
                post_logout_redirect_uri=post_logout, id_token_hint=id_token_hint
            )
            if url:
                target = url
        except oidc.OIDCError:
            pass  # fall back to local logout landing
    resp = RedirectResponse(target, status_code=302)
    resp.delete_cookie(
        key=settings.auth_cookie_name,
        path="/",
        domain=settings.auth_cookie_domain or None,
    )
    return resp


@router.get("/me", response_model=MeResponse)
async def me(user: User = Depends(current_user)):
    return _to_me(user)


@router.patch("/me", response_model=MeResponse)
async def update_me(
    payload: UpdateMeRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update profile fields. Currently editable: ``full_name``,
    ``preferences``. Email and admin flags are deliberately read-only —
    changing those is a separate flow (admin grants email change via
    /tenants, role changes are server-side only)."""
    if payload.full_name is not None:
        user.full_name = payload.full_name.strip() or None
    if payload.preferences is not None:
        # Merge rather than replace so a partial PATCH from one tab
        # doesn't blow away prefs set elsewhere.
        merged = dict(user.preferences or {})
        merged.update(payload.preferences)
        user.preferences = merged
    await db.commit()
    await db.refresh(user)
    return _to_me(user)


@router.post("/password", status_code=204)
async def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify the current password, then set a new one. Returns 204 on
    success; 400 if the current password is wrong or the new password
    is the same as the current one (no-op preventer)."""
    if not user.hashed_password:
        raise HTTPException(400, "this account has no password set")
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(400, "current password is incorrect")
    if verify_password(payload.new_password, user.hashed_password):
        raise HTTPException(400, "new password must differ from current")
    user.hashed_password = hash_password(payload.new_password)
    await db.commit()
    return Response(status_code=204)


# ── Personal access tokens ────────────────────────────────────────────


@router.get("/tokens", response_model=list[PatListItem])
async def list_tokens(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """List every PAT minted by the current user, newest first.
    Revoked tokens are included so the user can see they've already
    been retired — UI greys them out."""
    stmt = (
        select(PersonalAccessToken)
        .where(PersonalAccessToken.user_id == user.id)
        .order_by(PersonalAccessToken.created_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_pat_item(p) for p in rows]


@router.post("/tokens", response_model=PatCreateResponse, status_code=201)
async def create_token(
    payload: PatCreateRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mint a new PAT. The cleartext is returned exactly once; the
    server keeps only the sha256 hash. If the client loses the value
    they have to revoke and recreate."""
    cleartext = generate_pat()
    digest = hash_pat(cleartext)
    expires_at: datetime | None = None
    if payload.expires_in_days is not None:
        from datetime import timedelta

        expires_at = datetime.now(tz=timezone.utc) + timedelta(
            days=payload.expires_in_days
        )
    pat = PersonalAccessToken(
        user_id=user.id,
        tenant_id=user.tenant_id,
        name=payload.name.strip(),
        token_hash=digest,
        prefix=cleartext[:12],
        expires_at=expires_at,
    )
    db.add(pat)
    await db.commit()
    await db.refresh(pat)
    return PatCreateResponse(token=cleartext, **_to_pat_item(pat).model_dump())


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_token(
    token_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a PAT revoked. The row is kept (for audit), but the
    middleware will refuse any subsequent request bearing it."""
    try:
        tid = _uuid.UUID(token_id)
    except ValueError as e:
        raise HTTPException(404, "token not found") from e
    pat = await db.get(PersonalAccessToken, tid)
    if pat is None or pat.user_id != user.id:
        raise HTTPException(404, "token not found")
    if pat.revoked_at is None:
        pat.revoked_at = datetime.now(tz=timezone.utc)
        await db.commit()
    return Response(status_code=204)


# ── Invite redemption ──────────────────────────────────────────────────
#
# Both endpoints below are intentionally under /auth/* so the
# AuthGateMiddleware's existing "/api/v1/auth/* is public" rule
# covers them. An invitee has no session — the invite token IS the
# auth.


@router.get("/invite/{token}", response_model=InvitePreview)
async def preview_invite(token: str, db: AsyncSession = Depends(get_db)) -> InvitePreview:
    """Return the human-readable details of an invite by its token.

    Used by the front-end's accept-invite page to render "You're
    invited to <tenant> as <role>" before the user submits. Returns
    404 (deliberately, not 401/403) for any token that's invalid,
    expired, revoked, or already accepted — we don't want to confirm
    "this token used to be valid" to a phishing fisher.
    """
    from daalu_automation.core.invites import hash_invite_token
    from daalu_automation.models import Invite, Tenant

    digest = hash_invite_token(token)
    inv = (
        await db.execute(select(Invite).where(Invite.token_hash == digest))
    ).scalar_one_or_none()
    now = datetime.now(tz=timezone.utc)
    if (
        inv is None
        or inv.accepted_at is not None
        or inv.revoked_at is not None
        or inv.expires_at <= now
    ):
        raise HTTPException(404, "invite not found or no longer valid")

    tenant = await db.get(Tenant, inv.tenant_id)
    inviter = (
        await db.get(User, inv.invited_by_user_id)
        if inv.invited_by_user_id
        else None
    )
    return InvitePreview(
        email=inv.email,
        role=inv.role,
        tenant_name=tenant.name if tenant else "",
        inviter_name=(inviter.full_name or inviter.email) if inviter else None,
        message=inv.message,
        expires_at=inv.expires_at,
    )


@router.post("/redeem-invite", response_model=LoginResponse)
async def redeem_invite(
    payload: RedeemInviteRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """Create the User row from an accepted invite and sign them in.

    The flow:
      1. Look up the invite by token hash. Reject if not pending.
      2. Verify no active user with this email already exists for
         the tenant (defence — create_invite already checks).
      3. Hash the password and create the User row with the invite's
         role mapped to ``is_admin``.
      4. Mark the invite ``accepted_at`` + ``accepted_user_id``.
      5. Issue a session cookie identical to /login's.
      6. Return the same shape /login returns so the front-end can
         hand the response to the existing AuthProvider without a
         separate code path.
    """
    from daalu_automation.core.invites import hash_invite_token
    from daalu_automation.models import Invite

    digest = hash_invite_token(payload.token)
    inv = (
        await db.execute(select(Invite).where(Invite.token_hash == digest))
    ).scalar_one_or_none()
    now = datetime.now(tz=timezone.utc)
    if (
        inv is None
        or inv.accepted_at is not None
        or inv.revoked_at is not None
        or inv.expires_at <= now
    ):
        raise HTTPException(404, "invite not found or no longer valid")

    # Race-safe: the SELECT-then-INSERT pattern can lose if two
    # tabs redeem at the same moment, but the User unique index on
    # (tenant_id, email) catches duplicates. We translate that into
    # a 409 below.
    existing = (
        await db.execute(
            select(User).where(User.tenant_id == inv.tenant_id, User.email == inv.email)
        )
    ).scalar_one_or_none()
    if existing is not None and existing.is_active:
        raise HTTPException(409, "a user with this email already exists on the tenant")

    user = User(
        email=inv.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        is_admin=(inv.role == "admin"),
        is_active=True,
        tenant_id=inv.tenant_id,
    )
    db.add(user)
    try:
        await db.flush()
    except Exception:
        await db.rollback()
        raise HTTPException(409, "could not create user (likely duplicate)")

    inv.accepted_at = now
    inv.accepted_user_id = user.id
    await db.commit()
    await db.refresh(user)

    # Mirror the new user into Keycloak so their SSO login works the moment
    # the hub is in OIDC mode (the realm is the credential authority). The
    # password they just chose becomes their Keycloak password too, so the
    # transition is invisible. Best-effort: a mirror failure must not break
    # invite redemption — the backfill task / self-heal can reconcile later.
    await _mirror_user_to_keycloak(
        email=user.email, full_name=user.full_name, password=payload.password
    )

    # Issue the same session cookie /login does so the browser is
    # already logged in when it follows the redirect.
    token, expires_at = issue_token(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        is_admin=user.is_admin,
    )
    _set_session_cookie(response, token)
    return LoginResponse(token=token, expires_at=expires_at, user=_to_me(user))


# ── Self-service signup (local email + password) ───────────────────────
#
# Public (covered by the "/api/v1/auth/* is public" middleware rule). Gated
# behind ``self_signup_enabled``. A signup creates an isolated tenant + an
# inactive non-admin user (core/signup.py) and emails a verification link;
# redeeming it activates the account and logs the user straight in.


@router.post("/signup", response_model=SignupResponse, status_code=202)
async def signup(
    payload: SignupRequest,
    db: AsyncSession = Depends(get_db),
) -> SignupResponse:
    """Create a local account and send an email-verification link.

    Returns the same ``202 {"status": "verification_sent"}`` whether or not
    the email is already registered — no account enumeration. When the email
    is new we create the isolated tenant + inactive user and send the link;
    when it already exists we silently do nothing.
    """
    settings = get_settings()
    if not settings.self_signup_enabled:
        raise HTTPException(404, "self-service signup is not enabled")

    from daalu_automation.core import signup as signup_core

    email = payload.email.lower()
    existing = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        # Neutral response — don't reveal that this email is taken.
        return SignupResponse()

    try:
        user, cleartext = await signup_core.create_self_service_account(
            db,
            email=email,
            password=payload.password,
            full_name=payload.full_name,
            organization=payload.organization,
        )
    except Exception:
        # A racing duplicate (unique email) lands here — stay neutral.
        await db.rollback()
        return SignupResponse()

    await signup_core.send_verification_email(
        to_email=user.email,
        verify_url=signup_core.build_verify_url(cleartext),
        full_name=user.full_name,
        tenant_id=user.tenant_id,
        db=db,
    )
    return SignupResponse()


@router.post("/verify-email", response_model=LoginResponse)
async def verify_email(
    payload: VerifyEmailRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """Redeem a verification token: activate the account and sign the user in.

    404 for any token that's unknown, expired, or already consumed (we don't
    confirm "this token used to be valid"). On success the account is
    activated and we set the same session cookie /login does so the browser
    lands logged-in.
    """
    from daalu_automation.core.signup import hash_verification_token
    from daalu_automation.models import EmailVerificationToken

    digest = hash_verification_token(payload.token)
    tok = (
        await db.execute(
            select(EmailVerificationToken).where(
                EmailVerificationToken.token_hash == digest
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(tz=timezone.utc)
    if tok is None or tok.consumed_at is not None or tok.expires_at <= now:
        raise HTTPException(404, "verification link is invalid or has expired")

    user = await db.get(User, tok.user_id)
    if user is None:
        raise HTTPException(404, "verification link is invalid or has expired")

    tok.consumed_at = now
    user.is_active = True
    user.email_verified_at = now
    await db.commit()
    await db.refresh(user)

    token, expires_at = issue_token(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        is_admin=user.is_admin,
    )
    _set_session_cookie(response, token)
    return LoginResponse(token=token, expires_at=expires_at, user=_to_me(user))


@router.post("/verify-email/resend", status_code=202)
async def resend_verification(
    payload: ResendVerificationRequest,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Re-issue a verification link for an unverified account.

    Neutral 202 regardless of whether the email exists or is already verified,
    so this can't be used to probe account state.
    """
    settings = get_settings()
    if not settings.self_signup_enabled:
        raise HTTPException(404, "self-service signup is not enabled")

    from daalu_automation.core import signup as signup_core

    user = (
        await db.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()
    # Only resend for a real, still-unverified, password-backed account.
    if user is not None and not user.is_active and user.email_verified_at is None:
        cleartext = await signup_core.issue_verification_token(db, user)
        await signup_core.send_verification_email(
            to_email=user.email,
            verify_url=signup_core.build_verify_url(cleartext),
            full_name=user.full_name,
            tenant_id=user.tenant_id,
            db=db,
        )
    return Response(status_code=202)


async def _mirror_user_to_keycloak(
    *, email: str, full_name: str | None, password: str | None
) -> None:
    """Best-effort: ensure a Keycloak realm user exists for this daalu user.

    No-op unless ``oidc_mirror_users`` is set and admin credentials are
    configured. Never raises into the caller — mirroring is a convenience,
    not a correctness gate (the realm can also be reconciled by the
    backfill task in ``core/keycloak_backfill.py``).
    """
    settings = get_settings()
    if not settings.oidc_mirror_users:
        return
    try:
        from daalu_automation.core.keycloak_admin import KeycloakAdmin

        admin = KeycloakAdmin()
        first, _, last = (full_name or "").partition(" ")
        uid = await admin.ensure_user(
            email=email, first_name=first or None, last_name=last or None
        )
        if password:
            await admin.set_password(user_id=uid, password=password, temporary=False)
    except Exception as exc:  # pragma: no cover — convenience path
        import structlog

        structlog.get_logger(__name__).warning(
            "keycloak.mirror_failed", email=email, error=str(exc)
        )
