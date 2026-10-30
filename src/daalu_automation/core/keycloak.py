"""Keycloak machine-token helper (OIDC client-credentials grant).

The hub calls NVIDIA Config Manager's ``svc-*`` endpoints, which are
JWT-only (no OIDC redirect — built for machine clients). This module
mints a short-lived JWT per ``(issuer, client_id, audience)`` via the
client-credentials grant and caches it in-process until shortly before
expiry, so a burst of executor calls doesn't hammer Keycloak.

Per-tenant NV-CM stacks each trust the same daalu Keycloak issuer; the
JWT's ``roles`` claim maps to NV-CM RBAC (grant the hub an execute role
for ``DeployWorkflow``). See docs/design/nv-config-manager-integration.md §8.

This helper is intentionally dependency-light (httpx + settings) and is
imported by ``core/configmgr`` only — adding it changes no existing
behaviour.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import httpx
import structlog

from daalu_automation.config import get_settings

logger = structlog.get_logger(__name__)

# Refresh this many seconds before the token's stated expiry so an
# in-flight request never races the boundary.
_EXPIRY_SKEW_S = 30.0


@dataclass(frozen=True)
class _CacheKey:
    token_url: str
    client_id: str
    audience: str


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float  # monotonic clock seconds


# Process-local cache. Keyed so multiple tenants / audiences coexist.
_CACHE: dict[_CacheKey, _CachedToken] = {}

# Per-user exchanged-token cache, keyed by sha256(refresh_token).
_USER_CACHE: dict[str, _CachedToken] = {}


class KeycloakAuthError(RuntimeError):
    """Raised when a client-credentials token cannot be obtained."""


def _resolve_token_url() -> str:
    settings = get_settings()
    if settings.keycloak_token_url:
        return settings.keycloak_token_url
    if not settings.keycloak_issuer_url:
        raise KeycloakAuthError(
            "keycloak_issuer_url (or keycloak_token_url) is not configured; "
            "cannot mint a machine token for NV-CM svc-* endpoints"
        )
    issuer = settings.keycloak_issuer_url.rstrip("/")
    return f"{issuer}/protocol/openid-connect/token"


async def get_machine_token(
    *,
    client_id: str,
    client_secret: str,
    audience: str | None = None,
    token_url: str | None = None,
) -> str:
    """Return a cached or freshly-minted client-credentials access token.

    ``audience`` defaults to ``settings.keycloak_token_audience`` (the
    value NV-CM's SecurityPolicy validates). ``token_url`` defaults to the
    issuer-derived endpoint. Raises :class:`KeycloakAuthError` on failure.
    """
    settings = get_settings()
    aud = audience or settings.keycloak_token_audience
    url = token_url or _resolve_token_url()
    key = _CacheKey(token_url=url, client_id=client_id, audience=aud)

    cached = _CACHE.get(key)
    now = time.monotonic()
    if cached is not None and cached.expires_at - _EXPIRY_SKEW_S > now:
        return cached.access_token

    form = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    # Keycloak accepts the requested audience via the (RFC 8707) resource
    # / audience param; many realms map it through a client scope. Sending
    # it is harmless when the realm ignores it.
    if aud:
        form["audience"] = aud

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, data=form)
    except httpx.HTTPError as exc:  # network-level
        raise KeycloakAuthError(f"token request to {url} failed: {exc}") from exc

    if resp.status_code != 200:
        raise KeycloakAuthError(
            f"token endpoint {url} returned {resp.status_code}: {resp.text[:200]}"
        )

    body = resp.json()
    access_token = body.get("access_token")
    if not access_token:
        raise KeycloakAuthError(f"token response from {url} had no access_token")
    # Keycloak returns expires_in (seconds). Default conservatively if absent.
    expires_in = float(body.get("expires_in", 60))
    _CACHE[key] = _CachedToken(
        access_token=access_token,
        expires_at=now + expires_in,
    )
    logger.debug(
        "keycloak.token_minted",
        client_id=client_id,
        audience=aud,
        expires_in=expires_in,
    )
    return access_token


async def get_user_nvcm_token(
    *,
    refresh_token: str,
    nvcm_client_id: str,
    nvcm_client_secret: str,
    token_url: str | None = None,
) -> str:
    """Mint a per-user NV-CM access token via Keycloak token-exchange.

    1. Refresh the user's hub (``keycloak_hub_client_id``) token into a fresh
       access token. The stored token is an OFFLINE refresh token (login asks
       for ``offline_access``), so it stays active across the SSO-session idle
       timeout and is reusable for the life of the offline session.
    2. Standard-token-exchange it through ``nvcm_client_id`` (daalu-hub-nvcm),
       whose audience mapper stamps ``aud=nv-config-manager`` — yielding a token
       that carries the USER's identity (``preferred_username``/``email``) with
       the audience the tenant gateway + NV-CM auth plugin accept. Injecting it
       (vs the service-account client-credentials token) logs the user into the
       tool web UIs as themselves.

    Cached per refresh token until near expiry. Raises :class:`KeycloakAuthError`.
    """
    settings = get_settings()
    url = token_url or _resolve_token_url()
    key = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    now = time.monotonic()
    cached = _USER_CACHE.get(key)
    if cached is not None and cached.expires_at - _EXPIRY_SKEW_S > now:
        return cached.access_token

    refresh_form = {
        "grant_type": "refresh_token",
        "client_id": settings.keycloak_hub_client_id,
        "refresh_token": refresh_token,
    }
    if settings.keycloak_hub_client_secret:
        refresh_form["client_secret"] = settings.keycloak_hub_client_secret
    exchange_form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": nvcm_client_id,
        "client_secret": nvcm_client_secret,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r1 = await client.post(url, data=refresh_form)
            if r1.status_code != 200:
                raise KeycloakAuthError(
                    f"hub-token refresh returned {r1.status_code}: {r1.text[:160]}"
                )
            user_at = r1.json().get("access_token")
            if not user_at:
                raise KeycloakAuthError("hub-token refresh had no access_token")
            exchange_form["subject_token"] = user_at
            r2 = await client.post(url, data=exchange_form)
    except httpx.HTTPError as exc:
        raise KeycloakAuthError(f"user token-exchange to {url} failed: {exc}") from exc

    if r2.status_code != 200:
        raise KeycloakAuthError(
            f"token-exchange {url} returned {r2.status_code}: {r2.text[:200]}"
        )
    body = r2.json()
    access_token = body.get("access_token")
    if not access_token:
        raise KeycloakAuthError("token-exchange response had no access_token")
    expires_in = float(body.get("expires_in", 60))
    # Keycloak clamps an exchanged token's exp to the end of the subject's SSO
    # session and will happily return HTTP 200 with a token that is ALREADY
    # EXPIRED (negative expires_in) once that session is older than the realm's
    # ssoSessionMaxLifespan — the offline refresh token does not lift this. A
    # dead token injected downstream is a hard "401 Jwt is expired" from the
    # NV-CM gateway, whereas raising here reaches the caller's service-token
    # fallback and the console still opens (as the shared identity). Never
    # cache it either: a poisoned entry would outlive the failing request.
    if expires_in <= _EXPIRY_SKEW_S:
        raise KeycloakAuthError(
            f"token-exchange returned an already-expired token "
            f"(expires_in={expires_in:g}s); the user's Keycloak SSO session is "
            "older than the realm's ssoSessionMaxLifespan — raise it above the "
            "hub session lifetime (daalu ensure-keycloak-sso)"
        )
    _USER_CACHE[key] = _CachedToken(
        access_token=access_token, expires_at=now + expires_in
    )
    logger.debug("keycloak.user_token_exchanged", nvcm_client_id=nvcm_client_id)
    return access_token


def clear_token_cache() -> None:
    """Drop all cached tokens (used by tests + on credential rotation)."""
    _CACHE.clear()
    _USER_CACHE.clear()
