"""Credential-config sealing — edge cases beyond the base ssh round-trip.

The base case (plaintext ssh password sealed on write + resolvable) is
pinned in test_tenant_integrations.py; these cover the remaining
shapes: private keys, the three network_credentials secrets, and the
pass-through contract for callers that PUT ciphertext directly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from daalu_automation.core.crypto import decrypt_secret, encrypt_secret
from daalu_automation.models import Integration


async def _stored_config(db, tenant_id, provider: str) -> dict:
    row = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.provider == provider,
            )
        )
    ).scalar_one()
    return row.config or {}


@pytest.mark.asyncio(loop_scope="session")
async def test_ssh_private_key_sealed(client, db, seeded):
    r = await client.put(
        "/api/v1/integrations/config/ssh_credentials",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
        json={"config": {"user": "daalu", "private_key": "FAKEKEYPEM"}},
    )
    assert r.status_code == 200, r.text
    cfg = await _stored_config(db, seeded.tenant_a_id, "ssh_credentials")
    assert "private_key" not in cfg
    assert decrypt_secret(cfg["private_key_ciphertext"]) == "FAKEKEYPEM"


@pytest.mark.asyncio(loop_scope="session")
async def test_network_credentials_seal_all_three_secrets(client, db, seeded):
    r = await client.put(
        "/api/v1/integrations/config/network_credentials",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
        json={"config": {"user": "admin", "password": "pw",
                         "enable_password": "enable-pw"}},
    )
    assert r.status_code == 200, r.text
    cfg = await _stored_config(db, seeded.tenant_a_id, "network_credentials")
    assert "password" not in cfg and "enable_password" not in cfg
    assert decrypt_secret(cfg["password_ciphertext"]) == "pw"
    assert decrypt_secret(cfg["enable_password_ciphertext"]) == "enable-pw"
    assert cfg["user"] == "admin"  # non-secret fields stored verbatim


@pytest.mark.asyncio(loop_scope="session")
async def test_already_sealed_config_passes_through(client, db, seeded):
    """Callers that PUT ciphertext directly (hosted provisioning, tests)
    are untouched — sealing only rewrites the plaintext keys."""
    ct = encrypt_secret("pre-sealed")
    r = await client.put(
        "/api/v1/integrations/config/ssh_credentials",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
        json={"config": {"user": "ops", "password_ciphertext": ct}},
    )
    assert r.status_code == 200, r.text
    cfg = await _stored_config(db, seeded.tenant_a_id, "ssh_credentials")
    assert cfg["password_ciphertext"] == ct
