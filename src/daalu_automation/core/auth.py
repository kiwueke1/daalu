"""Password hashing + JWT session tokens.

Phase-2 auth foundation. The contract is intentionally minimal:

- ``hash_password`` / ``verify_password`` wrap passlib's bcrypt.
- ``issue_token`` / ``decode_token`` wrap PyJWT with HS256 signed by
  ``settings.secret_key``.

Tokens carry the user UUID (``sub``) and the tenant UUID (``tid``). The
API dependency in ``api/deps.py`` is the only place that consumes them —
everything else just reads ``request.state.user``.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from daalu_automation.config import get_settings

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
_ALG = "HS256"


def generate_ingest_api_key() -> str:
    """Generate a fresh per-tenant ingest API key.

    32 random bytes hex-encoded — matches the openssl rand -hex 32 the
    docs tell operators to run. The cleartext is shown to the operator
    once at creation; only the sha256 hash is persisted.
    """
    return secrets.token_hex(32)


def hash_ingest_api_key(key: str) -> str:
    """Hash a cleartext ingest key for storage / lookup.

    sha256 hex digest — fast (this runs on every webhook POST) and the
    key is already high-entropy so we don't need a slow KDF here.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _pwd.verify(password, hashed)
    except Exception:
        return False


# ── Personal access tokens ────────────────────────────────────────────
#
# Opaque tokens minted from /settings → API tokens. The cleartext is
# shown to the user once; only the sha256 hash is stored. The
# ``PAT_PREFIX`` namespace lets the auth gate cheaply distinguish a PAT
# from a JWT before doing a DB lookup.

PAT_PREFIX = "dpat_"


def generate_pat() -> str:
    """Return a fresh personal-access-token cleartext.

    24 random URL-safe bytes (~32 chars) namespaced with ``dpat_``.
    Same shape that GitHub PATs use — easy to spot in env files.
    """
    return f"{PAT_PREFIX}{secrets.token_urlsafe(24)}"


def hash_pat(token: str) -> str:
    """Hash a cleartext PAT for storage / lookup (sha256 hex)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_pat(token: str) -> bool:
    return token.startswith(PAT_PREFIX)


def issue_token(
    *,
    user_id: str,
    tenant_id: str,
    is_admin: bool,
    extra: dict | None = None,
) -> tuple[str, datetime]:
    """Return (encoded_token, absolute_expiry).

    ``extra`` adds extra claims to the session JWT — used to carry the user's
    encrypted Keycloak refresh token (``krt``) so the NV-CM tool proxy can mint
    a per-user downstream token via token-exchange. Keep ``extra`` small: the
    JWT lands in the ``daalu_session`` cookie.
    """
    settings = get_settings()
    expire = datetime.now(tz=timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload = {
        "sub": user_id,
        "tid": tenant_id,
        "adm": bool(is_admin),
        "exp": int(expire.timestamp()),
        "iat": int(datetime.now(tz=timezone.utc).timestamp()),
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, settings.secret_key, algorithm=_ALG)
    return token, expire


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired, or signed wrong."""


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[_ALG])
    except jwt.PyJWTError as e:
        raise TokenError(str(e)) from e


# ── Executor tokens ──────────────────────────────────────────────────
#
# The executor service is the only thing allowed to call
# ``core.change_proposals.execute()``. It authenticates itself by
# minting a JWT that carries a dedicated ``scp`` claim — distinct from
# user JWTs (which never carry it). Prompt-injection on the LLM agent
# therefore can't smuggle execute rights into the agent's own session:
# even with the user's bearer token, ``scp`` will be missing.
#
# Same signing key as user tokens (HS256, settings.secret_key) so the
# decoder can be shared; the gate is the claim shape, not the key.


def mint_executor_token(
    *,
    tenant_id: str,
    scope: str | None = None,
    ttl_seconds: int = 3600,
) -> tuple[str, datetime]:
    """Return ``(encoded_token, absolute_expiry)`` for the executor service.

    The ``scope`` argument defaults to ``settings.executor_jwt_scope`` so
    callers don't have to thread the setting through.
    """
    settings = get_settings()
    effective_scope = scope or settings.executor_jwt_scope
    expire = datetime.now(tz=timezone.utc) + timedelta(seconds=ttl_seconds)
    payload = {
        "tid": tenant_id,
        "scp": effective_scope,
        "exp": int(expire.timestamp()),
        "iat": int(datetime.now(tz=timezone.utc).timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=_ALG)
    return token, expire


def verify_executor_token(token: str) -> dict:
    """Decode + validate an executor token.

    Raises ``TokenError`` if the signature, expiry, or scope claim is
    wrong. Returns the decoded payload on success.
    """
    settings = get_settings()
    payload = decode_token(token)
    if payload.get("scp") != settings.executor_jwt_scope:
        raise TokenError("executor scope mismatch")
    if "tid" not in payload:
        raise TokenError("executor token missing tid")
    return payload
