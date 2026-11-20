"""Stored cloud-account config: secrets sealed at rest, opened on read.

AWS, GCP and Azure connections are Integration rows whose secrets used to sit
in ``config`` as plaintext. They are now stored as ``<field>_ciphertext``
(Fernet, see ``core.crypto``), like SSH and Redfish credentials:

- the write path (``PUT /integrations/config/{provider}``) seals them;
- the read path (:func:`load`) opens them for the cloud tools, and re-seals
  any row still holding plaintext from before this change;
- :func:`fingerprint` changes whenever the stored config changes, so the
  per-tenant client caches in ``cloud_aws/gcp/azure`` drop stale credentials
  after a key rotation instead of keeping them until a restart.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# provider -> {plaintext field: ciphertext field}
CLOUD_SECRETS: dict[str, dict[str, str]] = {
    "aws": {
        "secret_access_key": "secret_access_key_ciphertext",
        "session_token": "session_token_ciphertext",
    },
    "gcp": {"service_account_json": "service_account_json_ciphertext"},
    "azure": {"client_secret": "client_secret_ciphertext"},
}


def seal(provider: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Return ``cfg`` with each plaintext secret replaced by its ciphertext."""
    mapping = CLOUD_SECRETS.get(provider)
    if not mapping:
        return cfg
    from daalu_automation.core.crypto import encrypt_secret

    out = dict(cfg)
    for plain, ct_key in mapping.items():
        secret = out.pop(plain, None)
        if secret:
            if not isinstance(secret, str):
                secret = json.dumps(secret)
            out[ct_key] = encrypt_secret(secret)
    return out


def open_config(provider: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Return ``cfg`` with plaintext secrets restored from their ciphertext.

    A plaintext value already present wins (legacy rows, or a value just
    typed into a form). Ciphertext keys are dropped from the result.
    """
    mapping = CLOUD_SECRETS.get(provider)
    if not mapping:
        return dict(cfg)
    from daalu_automation.core.crypto import decrypt_secret

    out = dict(cfg)
    for plain, ct_key in mapping.items():
        ct = out.pop(ct_key, None)
        if ct and not out.get(plain):
            out[plain] = decrypt_secret(ct)
    return out


def has_secret(provider: str, cfg: dict[str, Any], field: str) -> bool:
    """True when ``field`` is set, as plaintext or as its ciphertext."""
    ct_key = CLOUD_SECRETS.get(provider, {}).get(field)
    return bool(cfg.get(field) or (ct_key and cfg.get(ct_key)))


def fingerprint(cfg: dict[str, Any]) -> str:
    """A short hash of the stored config, for cache keys."""
    raw = json.dumps(cfg, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


async def load(tenant_id: uuid.UUID, provider: str) -> tuple[dict[str, Any], str] | None:
    """Return ``(opened config, fingerprint)`` for the tenant's row, or None.

    A row still holding plaintext secrets is sealed in place on the way out.
    """
    from sqlalchemy import select

    from daalu_automation.database import AsyncSessionLocal
    from daalu_automation.models import Integration

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == provider,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        stored = dict(row.config or {})
        if any(stored.get(p) for p in CLOUD_SECRETS.get(provider, {})):
            stored = seal(provider, stored)
            row.config = stored
            try:
                await db.commit()
                logger.info("cloud_config.sealed_legacy_row", provider=provider)
            except Exception:  # noqa: BLE001 — sealing is best effort; reads still work
                await db.rollback()
                logger.exception("cloud_config.seal_failed", provider=provider)
    return open_config(provider, stored), fingerprint(stored)
