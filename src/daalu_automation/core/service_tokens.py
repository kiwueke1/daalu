"""Short-lived service-to-service tokens.

The inference-gateway and workspace-controller both need to know
*which tenant* a request belongs to without trusting the caller to
say so honestly. The pattern: ``daalu-api`` mints a short-lived JWT
that names the tenant + user + purpose, signed with a shared secret
that the receiving service can verify.

This is intentionally a *separate* surface from user-facing auth
(``core/auth.py``):

* Different signing key (``settings.service_token_secret_key``) so a
  user-session leak doesn't grant service-token power.
* Different audience (``aud="daalu-internal"``) so a mistakenly
  forwarded user JWT can't be replayed against the gateway.
* Very short TTL (60 s default) so even a stolen token is barely
  useful.

Receiving services depend on ``verify_service_token`` only — they
never reach into the JWT or trust the caller's headers for tenant
identity.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt
from pydantic import BaseModel

from daalu_automation.config import get_settings

_ALG = "HS256"
_AUDIENCE = "daalu-internal"

ServicePurpose = Literal[
    "chat",
    "classifier",
    "coding",
    "rca",
    "embeddings",
    # edge-rpc: hub → daalu-edge-data forwarding for tenant-scoped
    # requests. The edge ONLY accepts this purpose; the inference
    # gateway ONLY accepts the inference-related purposes. Each
    # receiver checks its allowed-purposes set, so a stolen
    # edge-rpc token can't be replayed against the gateway and
    # vice versa.
    "edge-rpc",
    # nautobot-provision: daalu-api → nautobot-controller. The
    # controller ONLY accepts this purpose, so an inference-gateway
    # token can't be replayed to provision/destroy Nautobot stacks.
    "nautobot-provision",
    # config-manager-provision: daalu-api → config-manager-controller.
    # Same isolation rationale as nautobot-provision — the NV-CM
    # controller only accepts this purpose.
    "config-manager-provision",
    # gpu-provision: daalu-api → gpu-controller. The gpu-controller
    # ONLY accepts this purpose, so no other service token can be
    # replayed to deploy/destroy a tenant's vLLM GPU stack.
    "gpu-provision",
]


class ServiceTokenClaims(BaseModel):
    """Verified claims returned by :func:`verify_service_token`.

    Fields mirror the JWT payload but are typed and validated. Receiving
    services should branch off these, never the raw payload.
    """

    tenant_id: str
    user_id: str
    purpose: ServicePurpose
    issued_at: datetime
    expires_at: datetime


def mint_service_token(
    *,
    tenant_id: str,
    user_id: str,
    purpose: ServicePurpose,
    ttl_seconds: int = 60,
) -> str:
    """Mint a short-lived service-to-service JWT.

    Callers (typically ``daalu-api``) pass the verified user context
    they already established (cookie → ``current_user``) so the gateway
    can trust the tenant attribution without a DB round-trip per call.
    """
    settings = get_settings()
    if not settings.service_token_secret_key:
        # Misconfiguration — refuse to mint a token that nothing on the
        # other side can verify. Better than silently failing-open.
        raise RuntimeError(
            "service_token_secret_key is not configured; cannot mint service token"
        )

    now = datetime.now(tz=timezone.utc)
    exp = now + timedelta(seconds=ttl_seconds)
    payload = {
        "tid": str(tenant_id),
        "sub": str(user_id),
        "purpose": purpose,
        "aud": _AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.service_token_secret_key, algorithm=_ALG)


class ServiceTokenError(Exception):
    """Raised by :func:`verify_service_token` for any verification failure.

    Receiving services should translate this to HTTP 401 with no detail
    leaked back to the caller — the only legitimate caller is another
    Daalu service, so a verification failure means a bug or an attack.
    """


def verify_service_token(token: str) -> ServiceTokenClaims:
    """Verify a service token and return its typed claims.

    Raises :class:`ServiceTokenError` on any failure (bad signature,
    expired, wrong audience, missing claim, malformed). Never logs the
    token or claims at error level — exception text only.
    """
    settings = get_settings()
    if not settings.service_token_secret_key:
        raise ServiceTokenError("service token key not configured on this service")
    try:
        payload = jwt.decode(
            token,
            settings.service_token_secret_key,
            algorithms=[_ALG],
            audience=_AUDIENCE,
            options={"require": ["exp", "iat", "sub", "aud"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise ServiceTokenError("token expired") from e
    except jwt.InvalidAudienceError as e:
        raise ServiceTokenError("wrong audience — likely a forwarded user token") from e
    except jwt.PyJWTError as e:
        raise ServiceTokenError(f"invalid token: {e}") from e

    tenant_id = payload.get("tid")
    purpose = payload.get("purpose")
    if not tenant_id:
        raise ServiceTokenError("missing tid claim")
    if purpose not in (
        "chat",
        "classifier",
        "coding",
        "rca",
        "embeddings",
        "edge-rpc",
        "nautobot-provision",
        "config-manager-provision",
        "gpu-provision",
    ):
        raise ServiceTokenError(f"unrecognized purpose: {purpose!r}")

    return ServiceTokenClaims(
        tenant_id=str(tenant_id),
        user_id=str(payload["sub"]),
        purpose=purpose,
        issued_at=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    )
