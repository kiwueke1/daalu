"""Stamping the hub's Keycloak service credential onto the Integration.

The hub calls every tenant's NV-CM as one shared confidential client. Nothing
in the provision path used to write that credential, so a freshly provisioned
stack could not authenticate and every tool console failed with "integration
has no Keycloak service credentials" until someone wrote it by hand — and since
deleting an environment drops the Integration row, that recurred on every
re-provision. The progress poll now stamps it, which means it has to be
idempotent (the poll runs every 30s and ``encrypt_secret`` is nonce-based) and
it must not blank an operator-written value on a deployment that has no secret
configured yet.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from daalu_automation.api.routers.onboarding import (
    _upsert_config_manager_integration,
)
from daalu_automation.config import get_settings
from daalu_automation.core.crypto import decrypt_secret, encrypt_secret
from daalu_automation.models import Integration

_URLS = {
    "config_store_url": "https://svc-config-store.t.cmtools.daalu.io",
    "render_url": "https://svc-render.t.cmtools.daalu.io",
    "workflow_url": "https://svc-workflow.t.cmtools.daalu.io",
    "ui": "https://t.cmtools.daalu.io",
}
# Deliberately not secret-shaped: a random-looking literal here trips
# gitleaks' generic-api-key rule and fails the security build. Allowlisted by
# exact value in .gitleaks.toml.
_SECRET = "not-a-real-secret-test-only"


@pytest.fixture
def nvcm_secret(monkeypatch):
    """Configure the shared NV-CM service-client secret."""
    monkeypatch.setenv("KEYCLOAK_NVCM_CLIENT_SECRET", _SECRET)
    get_settings.cache_clear()
    yield _SECRET
    monkeypatch.delenv("KEYCLOAK_NVCM_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()


async def _upsert(db, tenant_id):
    await _upsert_config_manager_integration(
        db,
        tenant_id=tenant_id,
        urls=_URLS,
        base_hostname="t.cmtools.daalu.io",
        components={},
        resolved_tunnel_id=None,
    )


async def _config(db, tenant_id) -> dict:
    row = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.provider == "config_manager",
            )
        )
    ).scalar_one()
    return dict(row.config or {})


async def test_credential_is_stamped_on_first_write(db, seeded, nvcm_secret):
    await _upsert(db, seeded.tenant_a_id)
    cfg = await _config(db, seeded.tenant_a_id)
    assert cfg["keycloak_client_id"] == "daalu-hub-nvcm"
    # Stored encrypted — the plaintext key is never read by the NV-CM client.
    assert decrypt_secret(cfg["keycloak_client_secret_ciphertext"]) == nvcm_secret


async def test_repeat_poll_does_not_rewrite_the_ciphertext(db, seeded, nvcm_secret):
    # The poll runs every 30s. encrypt_secret is nonce-based, so re-encrypting
    # unconditionally would churn the row forever.
    await _upsert(db, seeded.tenant_a_id)
    first = (await _config(db, seeded.tenant_a_id))[
        "keycloak_client_secret_ciphertext"
    ]
    await _upsert(db, seeded.tenant_a_id)
    assert (await _config(db, seeded.tenant_a_id))[
        "keycloak_client_secret_ciphertext"
    ] == first


async def test_unconfigured_secret_does_not_blank_an_operator_value(db, seeded):
    # A deployment that hasn't got the setting yet must leave a hand-written
    # credential alone rather than wiping it back to the broken state.
    await _upsert(db, seeded.tenant_a_id)
    row = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == seeded.tenant_a_id,
                Integration.provider == "config_manager",
            )
        )
    ).scalar_one()
    row.config = {
        **dict(row.config or {}),
        "keycloak_client_id": "daalu-hub-nvcm",
        "keycloak_client_secret_ciphertext": encrypt_secret("written-by-hand"),
    }
    await db.commit()

    await _upsert(db, seeded.tenant_a_id)  # no secret configured
    cfg = await _config(db, seeded.tenant_a_id)
    assert decrypt_secret(cfg["keycloak_client_secret_ciphertext"]) == "written-by-hand"


async def test_stale_credential_is_replaced(db, seeded, nvcm_secret):
    # A rotated secret (or junk left by a key rotation) must converge to the
    # configured value, not be treated as "already set".
    await _upsert(db, seeded.tenant_a_id)
    row = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == seeded.tenant_a_id,
                Integration.provider == "config_manager",
            )
        )
    ).scalar_one()
    row.config = {
        **dict(row.config or {}),
        "keycloak_client_secret_ciphertext": "not-decryptable",
    }
    await db.commit()

    await _upsert(db, seeded.tenant_a_id)
    cfg = await _config(db, seeded.tenant_a_id)
    assert decrypt_secret(cfg["keycloak_client_secret_ciphertext"]) == nvcm_secret
