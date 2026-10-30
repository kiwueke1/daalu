"""Coverage for per-device credentials override (PR 9).

Adds priority resolution on top of the existing transport-aware
resolver (PR 5):

  per-device row (config.device_id == device.id)
    > Nautobot custom-field pointer ({provider}_ref → row.name)
    > tenant-wide fallback (row with no config.device_id)

Tests pin the priority order, the backward-compat path (single
tenant-wide row still works as before), and the loud-failure case
(ref set with no matching row → LookupError, not silent fallback).
"""

from __future__ import annotations

import pytest

from daalu_automation.core import change_proposals as cps
from daalu_automation.core.crypto import encrypt_secret
from daalu_automation.core.sot.models import Device
from daalu_automation.models import Integration, IntegrationStatus


async def _seed_row(
    db,
    tenant_id,
    *,
    provider: str,
    name: str,
    config: dict,
):
    row = Integration(
        tenant_id=tenant_id,
        provider=provider,
        module="infra",
        name=name,
        status=IntegrationStatus.connected,
        config=config,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _device(
    *, id: str = "dev-1", transport: str = "linux_ssh", extra: dict | None = None
) -> Device:
    return Device(
        id=id,
        name=id,
        primary_ip="10.0.0.5",
        platform="linux" if transport == "linux_ssh" else "redfish",
        transport=transport,
        tags=[],
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# 1. Backward compat — single tenant-wide row, no device_id, no ref
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_single_tenant_wide_row_still_works(db, seeded):
    """Existing tenants with one ssh_credentials row continue to work
    unchanged after the multi-row refactor."""
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="ssh_credentials",
        name="tenant default",
        config={
            "user": "daalu",
            "private_key_ciphertext": encrypt_secret("FAKEKEY"),
        },
    )
    creds = await cps.resolve_credentials(db, seeded.tenant_a_id, _device())
    assert creds.user == "daalu"
    assert creds.private_key_pem == "FAKEKEY"
    assert creds.host == "10.0.0.5"


# ---------------------------------------------------------------------------
# 2. Per-device row wins over tenant-wide
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_per_device_row_wins_over_tenant_wide(db, seeded):
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="ssh_credentials",
        name="tenant default",
        config={"user": "daalu", "private_key_ciphertext": encrypt_secret("DEFAULT")},
    )
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="ssh_credentials",
        name="bastion override",
        config={
            "device_id": "dev-bastion",
            "user": "root",
            "private_key_ciphertext": encrypt_secret("HIGHTRUST"),
        },
    )
    creds = await cps.resolve_credentials(
        db, seeded.tenant_a_id, _device(id="dev-bastion")
    )
    assert creds.user == "root"
    assert creds.private_key_pem == "HIGHTRUST"

    # Different device, same tenant — falls through to tenant-wide.
    other = await cps.resolve_credentials(
        db, seeded.tenant_a_id, _device(id="dev-other")
    )
    assert other.user == "daalu"
    assert other.private_key_pem == "DEFAULT"


# ---------------------------------------------------------------------------
# 3. Named-credential ref points to a specific row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_named_credential_ref_resolves_by_name(db, seeded):
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="ssh_credentials",
        name="tenant default",
        config={"user": "daalu", "private_key_ciphertext": encrypt_secret("DEFAULT")},
    )
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="ssh_credentials",
        name="bastion-pool",
        config={"user": "bastion", "private_key_ciphertext": encrypt_secret("POOL")},
    )
    dev = _device(extra={"ssh_credentials_ref": "bastion-pool"})
    creds = await cps.resolve_credentials(db, seeded.tenant_a_id, dev)
    assert creds.user == "bastion"
    assert creds.private_key_pem == "POOL"


# ---------------------------------------------------------------------------
# 4. Per-device row wins even when a ref is also set (priority order)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_per_device_row_wins_over_named_ref(db, seeded):
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="ssh_credentials",
        name="bastion-pool",
        config={"user": "bastion", "private_key_ciphertext": encrypt_secret("POOL")},
    )
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="ssh_credentials",
        name="exact override",
        config={
            "device_id": "dev-special",
            "user": "root",
            "private_key_ciphertext": encrypt_secret("EXACT"),
        },
    )
    dev = _device(id="dev-special", extra={"ssh_credentials_ref": "bastion-pool"})
    creds = await cps.resolve_credentials(db, seeded.tenant_a_id, dev)
    # Per-device row wins; the ref is irrelevant when an exact match exists.
    assert creds.user == "root"
    assert creds.private_key_pem == "EXACT"


# ---------------------------------------------------------------------------
# 5. Ref set with no matching row → LookupError (loud failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_unresolved_ref_raises(db, seeded):
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="ssh_credentials",
        name="tenant default",
        config={"user": "daalu", "private_key_ciphertext": encrypt_secret("X")},
    )
    dev = _device(extra={"ssh_credentials_ref": "does-not-exist"})
    with pytest.raises(LookupError, match="does-not-exist"):
        await cps.resolve_credentials(db, seeded.tenant_a_id, dev)


# ---------------------------------------------------------------------------
# 6. Only per-device rows, no tenant-wide → resolves only for matched device
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_only_per_device_rows_with_no_match_raises(db, seeded):
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="ssh_credentials",
        name="bastion override",
        config={
            "device_id": "dev-bastion",
            "user": "root",
            "private_key_ciphertext": encrypt_secret("HIGHTRUST"),
        },
    )
    # Different device, no tenant-wide row, no ref → loud error.
    with pytest.raises(LookupError, match="target a specific device"):
        await cps.resolve_credentials(db, seeded.tenant_a_id, _device(id="dev-other"))


# ---------------------------------------------------------------------------
# 7. Symmetric for Redfish — per-device row + redfish_credentials_ref
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_redfish_per_device_row(db, seeded):
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="redfish_credentials",
        name="tenant BMC default",
        config={"user": "admin", "password_ciphertext": encrypt_secret("default")},
    )
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="redfish_credentials",
        name="prod BMC override",
        config={
            "device_id": "bmc-prod-01",
            "user": "root",
            "password_ciphertext": encrypt_secret("calvin"),
        },
    )
    creds = await cps.resolve_credentials(
        db, seeded.tenant_a_id,
        _device(id="bmc-prod-01", transport="redfish"),
    )
    assert creds.user == "root"
    assert creds.password == "calvin"
    assert creds.port == 443  # Redfish defaults preserved


@pytest.mark.asyncio(loop_scope="session")
async def test_redfish_named_ref(db, seeded):
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="redfish_credentials",
        name="tenant BMC default",
        config={"user": "admin", "password_ciphertext": encrypt_secret("default")},
    )
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="redfish_credentials",
        name="vendor-svc",
        config={"user": "svc-account", "password_ciphertext": encrypt_secret("svc-pw")},
    )
    dev = _device(
        id="bmc-x",
        transport="redfish",
        extra={"redfish_credentials_ref": "vendor-svc"},
    )
    creds = await cps.resolve_credentials(db, seeded.tenant_a_id, dev)
    assert creds.user == "svc-account"
    assert creds.password == "svc-pw"


# ---------------------------------------------------------------------------
# 8. Custom-field user/port overrides apply on top of the *selected* row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_user_port_overrides_apply_after_row_selection(db, seeded):
    await _seed_row(
        db, seeded.tenant_a_id,
        provider="ssh_credentials",
        name="bastion-pool",
        config={
            "user": "bastion",
            "port": 22,
            "private_key_ciphertext": encrypt_secret("POOL"),
        },
    )
    dev = _device(
        extra={
            "ssh_credentials_ref": "bastion-pool",
            "ssh_user": "deploy",  # device-level override
            "ssh_port": 2222,
        },
    )
    creds = await cps.resolve_credentials(db, seeded.tenant_a_id, dev)
    # Row picked by ref, but device-level overrides win for user/port.
    assert creds.user == "deploy"
    assert creds.port == 2222
    # Key still comes from the picked row.
    assert creds.private_key_pem == "POOL"
