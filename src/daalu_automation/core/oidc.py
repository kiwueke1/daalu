"""OIDC relying-party helper for the hub's *human* login flow.

Distinct from ``core/keycloak.py`` (which mints machine client-credentials
tokens for NV-CM ``svc-*`` calls). This module is the browser-facing
authorization-code + PKCE flow that lets an operator log into
``ops.daalu.io`` through the same Keycloak realm the NV-CM tools trust —
so the browser already holds a Keycloak SSO session and the deep links
into Nautobot / the NV-CM UIs are seamless (no second prompt).

The contract is intentionally small and async-friendly (httpx + PyJWT,
no extra deps):

- :func:`discovery` — cached ``.well-known/openid-configuration`` fetch.
- :func:`build_authorize_url` — the redirect to Keycloak.
- :func:`exchange_code` — code → token set at the token endpoint.
- :func:`validate_id_token` — verify signature (JWKS), ``iss``, ``aud``,
  ``exp``, and the login ``nonce``; return the claims.

Identity is *not* established here — the caller maps the verified
``email`` claim onto a daalu ``User`` row and issues the normal
``daalu_session`` cookie. Keycloak is the credential authority; daalu
remains the system of record for who a user is and their tenant/role.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass

import httpx
import jwt
import structlog
from jwt.algorithms import RSAAlgorithm

from daalu_automation.config import get_settings

logger = structlog.get_logger(__name__)

# OIDC discovery + JWKS change rarely; cache them so login isn't gated on
# two extra round-trips to Keycloak every time.
_DISCOVERY_TTL_S = 600.0
_JWKS_TTL_S = 600.0


class OIDCError(RuntimeError):
    """Raised when the OIDC flow cannot be completed (config or protocol)."""


@dataclass
class _Cached:
    value: dict
    expires_at: float  # monotonic seconds


_discovery_cache: dict[str, _Cached] = {}
_jwks_cache: dict[str, _Cached] = {}


def _issuer() -> str:
    issuer = (get_settings().keycloak_issuer_url or "").rstrip("/")
    if not issuer:
        raise OIDCError(
            "keycloak_issuer_url is not configured; hub OIDC login is unavailable"
        )
    return issuer


async def discovery() -> dict:
    """Return the realm's OIDC discovery document (cached)."""
    issuer = _issuer()
    cached = _discovery_cache.get(issuer)
    now = time.monotonic()
    if cached is not None and cached.expires_at > now:
        return cached.value
    url = f"{issuer}/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise OIDCError(f"OIDC discovery fetch from {url} failed: {exc}") from exc
    if resp.status_code != 200:
        raise OIDCError(f"OIDC discovery {url} returned {resp.status_code}")
    doc = resp.json()
    _discovery_cache[issuer] = _Cached(value=doc, expires_at=now + _DISCOVERY_TTL_S)
    return doc


async def _jwks() -> dict:
    issuer = _issuer()
    cached = _jwks_cache.get(issuer)
    now = time.monotonic()
    if cached is not None and cached.expires_at > now:
        return cached.value
    jwks_uri = (await discovery()).get("jwks_uri")
    if not jwks_uri:
        raise OIDCError("OIDC discovery document has no jwks_uri")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(jwks_uri)
    except httpx.HTTPError as exc:
        raise OIDCError(f"JWKS fetch from {jwks_uri} failed: {exc}") from exc
    if resp.status_code != 200:
        raise OIDCError(f"JWKS {jwks_uri} returned {resp.status_code}")
    jwks = resp.json()
    _jwks_cache[issuer] = _Cached(value=jwks, expires_at=now + _JWKS_TTL_S)
    return jwks


# ── PKCE + state helpers ──────────────────────────────────────────────


def new_state() -> str:
    return secrets.token_urlsafe(24)


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def new_code_verifier() -> str:
    # RFC 7636: 43–128 chars of unreserved set. token_urlsafe(64)≈86 chars.
    return secrets.token_urlsafe(64)


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def build_authorize_url(
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_verifier: str,
    idp_hint: str | None = None,
) -> str:
    """Build the Keycloak authorization-endpoint URL (PKCE S256).

    When ``idp_hint`` is set (e.g. ``"google"``) we pass Keycloak's
    ``kc_idp_hint`` parameter. Keycloak then skips its own login page and
    redirects straight to that identity provider — so the user never sees
    Keycloak's UI, only Daalu's login screen and then Google. The alias must
    match the Keycloak IdP alias (see ``keycloak_admin.ensure_identity_provider_google``).
    """
    import urllib.parse

    settings = get_settings()
    auth_ep = (await discovery()).get("authorization_endpoint")
    if not auth_ep:
        raise OIDCError("OIDC discovery document has no authorization_endpoint")
    params = {
        "client_id": settings.keycloak_hub_client_id,
        "response_type": "code",
        # ``offline_access`` makes Keycloak return an OFFLINE refresh token. The
        # NV-CM tool proxy stores this token (as the session's ``krt``) and
        # token-exchanges it per request to log the user into the tools as
        # themselves. An ONLINE refresh token dies after the realm's 30-minute
        # SSO-session idle timeout, so a user who browsed Daalu for a while then
        # opened a tool got bounced to the tool's own login (the proxy fell back
        # to the service-account token). Offline tokens are governed by the far
        # longer ``offlineSessionIdleTimeout`` (~30 days), so the per-user path
        # keeps working across an ordinary session. ``offline_access`` is an
        # optional client scope Keycloak grants without a consent prompt when
        # the client has "Consent Required" off (daalu-hub-ui does).
        "scope": "openid email profile offline_access",
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge_s256(code_verifier),
        "code_challenge_method": "S256",
    }
    if idp_hint:
        params["kc_idp_hint"] = idp_hint
    return f"{auth_ep}?{urllib.parse.urlencode(params)}"


async def exchange_code(*, code: str, redirect_uri: str, code_verifier: str) -> dict:
    """Exchange an authorization code for the token set."""
    settings = get_settings()
    token_ep = (await discovery()).get("token_endpoint")
    if not token_ep:
        raise OIDCError("OIDC discovery document has no token_endpoint")
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": settings.keycloak_hub_client_id,
        "client_secret": settings.keycloak_hub_client_secret,
        "code_verifier": code_verifier,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(token_ep, data=form)
    except httpx.HTTPError as exc:
        raise OIDCError(f"token exchange to {token_ep} failed: {exc}") from exc
    if resp.status_code != 200:
        raise OIDCError(
            f"token endpoint {token_ep} returned {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()


async def validate_id_token(id_token: str, *, nonce: str) -> dict:
    """Verify an ID token's signature + claims and return them.

    Verifies the RS256 signature against the realm JWKS (matched by
    ``kid``), the issuer, the audience (the hub client id), expiry, and
    that the ``nonce`` matches the one minted at login (replay defence).
    """
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OIDCError(f"malformed id_token header: {exc}") from exc
    kid = header.get("kid")
    jwks = await _jwks()
    key_obj = None
    for jwk in jwks.get("keys", []):
        if jwk.get("kid") == kid:
            key_obj = RSAAlgorithm.from_jwk(jwk)
            break
    if key_obj is None:
        # kid rotated since we cached — refresh once and retry.
        _jwks_cache.pop(_issuer(), None)
        for jwk in (await _jwks()).get("keys", []):
            if jwk.get("kid") == kid:
                key_obj = RSAAlgorithm.from_jwk(jwk)
                break
    if key_obj is None:
        raise OIDCError(f"no JWKS key matches id_token kid={kid}")
    try:
        claims = jwt.decode(
            id_token,
            key=key_obj,
            algorithms=["RS256"],
            audience=settings.keycloak_hub_client_id,
            issuer=_issuer(),
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise OIDCError(f"id_token validation failed: {exc}") from exc
    if claims.get("nonce") != nonce:
        raise OIDCError("id_token nonce mismatch (possible replay)")
    return claims


async def end_session_url(*, post_logout_redirect_uri: str, id_token_hint: str | None = None) -> str | None:
    """Return the RP-initiated logout URL, or None if unsupported."""
    import urllib.parse

    settings = get_settings()
    end_ep = (await discovery()).get("end_session_endpoint")
    if not end_ep:
        return None
    params = {
        "client_id": settings.keycloak_hub_client_id,
        "post_logout_redirect_uri": post_logout_redirect_uri,
    }
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    return f"{end_ep}?{urllib.parse.urlencode(params)}"


def clear_caches() -> None:
    """Drop discovery + JWKS caches (tests / key rotation)."""
    _discovery_cache.clear()
    _jwks_cache.clear()
