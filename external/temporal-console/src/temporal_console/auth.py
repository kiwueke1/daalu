"""Keycloak OIDC auth via Authlib.

Authorization-code flow with a signed session cookie holding the
access + refresh tokens. When `OIDC_ISSUER` is empty (local dev), auth
is fully bypassed — every request behaves as an authenticated user.

Middleware pattern: we set `request.state.user` on every request; route
handlers can call `require_auth(request)` to reject unauthenticated
calls with 302 → /auth/login.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import structlog
from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from temporal_console.config import get_settings

logger = structlog.get_logger(__name__)
_settings = get_settings()


_SESSION_COOKIE_NAME = "tc_session"
_SESSION_TTL_SECS = 60 * 60 * 8  # 8 hours


# ─────────────────────────────────────────────────────────────────────────
# Session cookie helpers
# ─────────────────────────────────────────────────────────────────────────

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_settings.session_secret, salt="tc-session")


def encode_session(user: dict[str, Any]) -> str:
    return _serializer().dumps(user)


def decode_session(token: str) -> dict[str, Any] | None:
    try:
        return _serializer().loads(token, max_age=_SESSION_TTL_SECS)
    except BadSignature:
        return None
    except Exception:
        return None


def set_session_cookie(response: Response, user: dict[str, Any]) -> None:
    response.set_cookie(
        key=_SESSION_COOKIE_NAME,
        value=encode_session(user),
        max_age=_SESSION_TTL_SECS,
        httponly=True,
        secure=_settings.is_production,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(_SESSION_COOKIE_NAME)


# ─────────────────────────────────────────────────────────────────────────
# OAuth client
# ─────────────────────────────────────────────────────────────────────────

oauth = OAuth()
if _settings.auth_enabled:
    oauth.register(
        name="keycloak",
        client_id=_settings.oidc_client_id,
        client_secret=_settings.oidc_client_secret,
        server_metadata_url=f"{_settings.oidc_issuer}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )


# ─────────────────────────────────────────────────────────────────────────
# Request-time auth resolution
# ─────────────────────────────────────────────────────────────────────────

def _dev_user() -> dict[str, Any]:
    """Synthetic user for local dev when OIDC is disabled."""
    return {"sub": "dev", "email": "dev@local", "name": "Dev", "roles": ["admin"]}


def resolve_user(request: Request) -> dict[str, Any] | None:
    """Return the current user dict, or None if unauthenticated.

    When auth is disabled (no issuer configured), returns a dev user so
    route handlers don't have to branch. In-cluster deployments MUST set
    OIDC_ISSUER.
    """
    if not _settings.auth_enabled:
        return _dev_user()
    token = request.cookies.get(_SESSION_COOKIE_NAME)
    if not token:
        return None
    user = decode_session(token)
    if user is None:
        return None
    if user.get("expires_at") and user["expires_at"] < time.time():
        return None
    if _settings.oidc_required_role:
        roles = user.get("roles") or []
        if _settings.oidc_required_role not in roles:
            return None
    return user


def require_auth(request: Request) -> dict[str, Any]:
    """FastAPI dep: 302 → /auth/login for unauthenticated GETs, 401 JSON for non-GET."""
    user = resolve_user(request)
    if user is not None:
        return user

    if request.method == "GET":
        # Preserve the target URL through the OIDC roundtrip.
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        # Raising an HTTPException with a Location header via detail is hacky;
        # cleaner to raise a RedirectResponse from the route. Callers use
        # the `current_user` dep below + handle unauthenticated themselves.
        raise _Unauthenticated(next_url)

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


class _Unauthenticated(Exception):
    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


async def handle_unauthenticated(request: Request, exc: _Unauthenticated) -> Response:
    """Starlette exception handler — turn `_Unauthenticated` into a 302."""
    return RedirectResponse(
        url=f"/auth/login?next={exc.next_url}",
        status_code=status.HTTP_302_FOUND,
    )


# ─────────────────────────────────────────────────────────────────────────
# Auth route handlers (wired by main.py)
# ─────────────────────────────────────────────────────────────────────────

async def login(request: Request):
    """Start the authorization-code flow with Keycloak."""
    if not _settings.auth_enabled:
        return RedirectResponse(url="/")
    next_url = request.query_params.get("next", "/")
    # Stash next_url in a cookie — session middleware can't be used here
    # because we haven't started a session yet.
    state = secrets.token_urlsafe(16)
    redirect_uri = _settings.oidc_redirect_uri
    response = await oauth.keycloak.authorize_redirect(request, redirect_uri, state=state)
    response.set_cookie(
        "tc_next", next_url, max_age=600, httponly=True,
        secure=_settings.is_production, samesite="lax",
    )
    return response


async def callback(request: Request):
    """Handle the Keycloak redirect — exchange code for tokens, set session."""
    if not _settings.auth_enabled:
        return RedirectResponse(url="/")
    try:
        token = await oauth.keycloak.authorize_access_token(request)
    except Exception as exc:
        logger.warning("auth.callback_failed", error=str(exc))
        return RedirectResponse(url="/auth/login")
    userinfo = token.get("userinfo") or {}
    access = token.get("access_token") or ""
    # Decode realm roles from the access-token claims if present.
    realm_access = (token.get("id_token_claims") or {}).get("realm_access") or {}
    roles = realm_access.get("roles") or []
    user = {
        "sub": userinfo.get("sub") or "unknown",
        "email": userinfo.get("email", ""),
        "name": userinfo.get("name") or userinfo.get("preferred_username") or userinfo.get("email", ""),
        "roles": roles,
        "expires_at": int(time.time()) + _SESSION_TTL_SECS,
    }
    next_url = request.cookies.get("tc_next") or "/"
    resp = RedirectResponse(url=next_url, status_code=status.HTTP_302_FOUND)
    set_session_cookie(resp, user)
    resp.delete_cookie("tc_next")
    logger.info("auth.login_success", email=user["email"])
    return resp


async def logout(request: Request):
    resp = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    clear_session_cookie(resp)
    return resp
