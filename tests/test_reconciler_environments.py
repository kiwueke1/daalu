"""Drift reconciliation and execution across several NV-CM environments.

Each management environment bundles its own Nautobot, so a device id is only
unique *within* an environment. These tests pin the four properties that make
multi-environment drift work — and that were all broken while the workers
resolved a single tenant-default source of truth:

1. ``_environments_for`` sweeps every environment that has a Nautobot, and
   falls back to the tenant default only when there are no environments.
2. ``_has_open_proposal`` is environment-scoped, so an open proposal in one
   fabric cannot mask drift on a same-named device in another.
3. ``propose`` records the environment, so the row says which fabric it is
   about.
4. The executor re-resolves each proposal in *its own* environment rather
   than the tenant default.

The eligibility gate is covered too: an NV-CM tenant has no BYO ``nautobot``
integration row at all, so gating on that alone skipped those tenants
entirely.
"""

from __future__ import annotations

import pytest

from daalu_automation.config_manager_controller import local_admin
from daalu_automation.core import change_proposals as cps
from daalu_automation.core.sot.models import Actor
from daalu_automation.models import (
    ChangeProposalKind,
    ChangeProposalStatus,
    ConfigManagerTenant,
    ConfigManagerTenantState,
)
from daalu_automation.workers.reconciler import (
    _environments_for,
    _has_open_proposal,
)


async def _add_environment(db, tenant_id, *, slug, nautobot_url=None):
    row = ConfigManagerTenant(
        tenant_id=tenant_id,
        name=slug.title(),
        slug=slug,
        is_primary=False,
        state=ConfigManagerTenantState.active,
        namespace=f"cm-default-{slug}",
        base_hostname=f"{slug}-default.cmtools.test",
        urls={"nautobot_url": nautobot_url} if nautobot_url else {},
    )
    row.secrets_ciphertext = local_admin.with_api_token(row, "tok")
    db.add(row)
    await db.commit()
    return row


async def _propose(db, tenant_id, *, device_id, env, user_id=None):
    return await cps.propose(
        db,
        tenant_id,
        device_id=device_id,
        environment_slug=env,
        kind=ChangeProposalKind.drift,
        intended_config="",
        observed_config="",
        diff="",
        renderer_version="v1",
        evidence={},
        actor=Actor(kind="system", name="test"),
    )


# ── 1. Which fabrics get swept ───────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_environments_for_falls_back_to_default_when_none_exist(
    db, seeded
):
    """A BYO tenant has no environments — sweep the tenant default."""
    assert await _environments_for(db, seeded.tenant_b_id) == [None]


@pytest.mark.asyncio(loop_scope="session")
async def test_environments_for_lists_every_environment_with_a_nautobot(
    db, seeded
):
    await _add_environment(
        db, seeded.tenant_a_id, slug="lab", nautobot_url="http://n1.test"
    )
    await _add_environment(
        db, seeded.tenant_a_id, slug="prod", nautobot_url="http://n2.test"
    )
    # Still provisioning: no Nautobot published yet, so nothing to sweep.
    await _add_environment(db, seeded.tenant_a_id, slug="half")

    envs = await _environments_for(db, seeded.tenant_a_id)
    assert sorted(e for e in envs if e) == ["lab", "prod"]
    # The tenant default is NOT also swept: with environments present it
    # resolves to the primary's Nautobot, which is already in the list.
    assert None not in envs


# ── 2. Open-proposal check is per environment ────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_open_proposal_in_one_environment_does_not_mask_another(
    db, seeded, fake_redis
):
    """The same device id in two fabrics is two different devices."""
    device_id = "same-id-different-fabric"
    await _propose(db, seeded.tenant_a_id, device_id=device_id, env="lab")

    assert await _has_open_proposal(db, seeded.tenant_a_id, device_id, "lab")
    # Different environment → not masked, so drift there still gets proposed.
    assert not await _has_open_proposal(
        db, seeded.tenant_a_id, device_id, "prod"
    )
    # …and neither is the tenant-default fabric.
    assert not await _has_open_proposal(
        db, seeded.tenant_a_id, device_id, None
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_default_environment_proposal_is_found_by_null_lookup(
    db, seeded, fake_redis
):
    """NULL is a real value here, not 'unknown' — match it with IS NULL."""
    device_id = "default-fabric-device"
    await _propose(db, seeded.tenant_a_id, device_id=device_id, env=None)

    assert await _has_open_proposal(db, seeded.tenant_a_id, device_id, None)
    assert not await _has_open_proposal(
        db, seeded.tenant_a_id, device_id, "lab"
    )


# ── 3. The row records its fabric ────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_propose_records_the_environment(db, seeded, fake_redis):
    row = await _propose(
        db, seeded.tenant_a_id, device_id="dev-1", env="lab"
    )
    await db.refresh(row)
    assert row.environment_slug == "lab"

    default_row = await _propose(
        db, seeded.tenant_a_id, device_id="dev-2", env=None
    )
    await db.refresh(default_row)
    assert default_row.environment_slug is None


# ── 4. The executor follows the proposal's environment ───────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_executor_resolves_each_proposal_in_its_own_environment(
    db, seeded, fake_redis, monkeypatch
):
    """Two approved rows in two fabrics must not share one SoT."""
    from daalu_automation.workers import executor as ex

    seen: list[str | None] = []

    class _RecordingSoT:
        def __init__(self, env=None):
            seen.append(env)

        async def get_device(self, *_a, **_kw):
            # Stop right after resolution — the point under test is which
            # Nautobot got asked, not what came back.
            return None

    monkeypatch.setattr(ex, "NautobotSoT", _RecordingSoT)

    for device_id, env in (("d-lab", "lab"), ("d-prod", "prod")):
        row = await _propose(
            db, seeded.tenant_a_id, device_id=device_id, env=env
        )
        await cps.approve(
            db,
            seeded.tenant_a_id,
            row.id,
            actor=Actor(kind="user", user_id=seeded.user_a_id),
        )

    stats = await ex._execute_approved_batch()

    assert sorted(e for e in seen if e) == ["lab", "prod"], seen
    # Both were resolved (and skipped, since the stub reports no device).
    assert stats["processed"] == 2


# ── Eligibility: an NV-CM tenant has no BYO nautobot row ─────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_config_manager_tenant_is_eligible_without_a_byo_nautobot_row(
    db, seeded, ssh_credentials_integration
):
    """The bundled Nautobot arrives via config_manager, not `nautobot`."""
    from daalu_automation.models import Integration, IntegrationStatus
    from daalu_automation.workers.reconciler import _eligible_tenant_ids

    db.add(
        Integration(
            tenant_id=seeded.tenant_a_id,
            provider="config_manager",
            module="infra",
            name="NV Config Manager",
            status=IntegrationStatus.connected,
            config={"nautobot_url": "http://svc-nautobot.test"},
        )
    )
    await db.commit()

    assert seeded.tenant_a_id in await _eligible_tenant_ids()


@pytest.mark.asyncio(loop_scope="session")
async def test_tenant_without_ssh_credentials_is_still_ineligible(db, seeded):
    """No credential, nothing to reconcile with — gate stays shut."""
    from daalu_automation.workers.reconciler import _eligible_tenant_ids

    assert seeded.tenant_b_id not in await _eligible_tenant_ids()


@pytest.mark.asyncio(loop_scope="session")
async def test_executed_proposal_no_longer_counts_as_open(
    db, seeded, fake_redis
):
    """Only pending/approved block a fresh drift proposal."""
    row = await _propose(
        db, seeded.tenant_a_id, device_id="dev-closed", env="lab"
    )
    row.status = ChangeProposalStatus.executed
    await db.commit()

    assert not await _has_open_proposal(
        db, seeded.tenant_a_id, "dev-closed", "lab"
    )
