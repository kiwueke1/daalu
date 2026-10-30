"""Persisting the tenant gateway address onto the config_manager Integration.

The tool proxy reads ``gateway_internal_url`` out of the Integration config on
every request; until it is set, clicking any NV-CM tool fails with "in-cluster
gateway address not provisioned". The controller reports the address once Envoy
Gateway has provisioned the proxy Service, so the progress poll is what writes
it — which means the poll must tolerate the mid-install window where the
controller has nothing to report yet without blanking what's already there.
"""

from __future__ import annotations

from sqlalchemy import select

from daalu_automation.api.routers.onboarding import (
    _upsert_config_manager_integration,
)
from daalu_automation.models import Integration

_URLS = {
    "config_store_url": "https://svc-config-store.t.cmtools.daalu.io",
    "render_url": "https://svc-render.t.cmtools.daalu.io",
    "workflow_url": "https://svc-workflow.t.cmtools.daalu.io",
    "ui": "https://t.cmtools.daalu.io",
}
_GW = "http://envoy-cm-t-gw-abc.envoy-gateway-system.svc.cluster.local"


async def _upsert(db, tenant_id, *, gateway_internal_url=None):
    await _upsert_config_manager_integration(
        db,
        tenant_id=tenant_id,
        urls=_URLS,
        base_hostname="t.cmtools.daalu.io",
        components={},
        gateway_internal_url=gateway_internal_url,
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


async def test_gateway_url_is_persisted_on_first_write(db, seeded):
    await _upsert(db, seeded.tenant_a_id, gateway_internal_url=_GW)
    assert (await _config(db, seeded.tenant_a_id))["gateway_internal_url"] == _GW


async def test_mid_install_poll_does_not_blank_a_known_gateway_url(db, seeded):
    # The regression that matters: a later poll (or a controller that briefly
    # can't reach the cluster) reports None, and a naive merge would wipe the
    # address, breaking every tool link until someone backfilled it by hand.
    await _upsert(db, seeded.tenant_a_id, gateway_internal_url=_GW)
    await _upsert(db, seeded.tenant_a_id, gateway_internal_url=None)
    assert (await _config(db, seeded.tenant_a_id))["gateway_internal_url"] == _GW


async def test_gateway_url_lands_on_a_later_poll(db, seeded):
    # First poll runs while Envoy Gateway is still provisioning.
    await _upsert(db, seeded.tenant_a_id, gateway_internal_url=None)
    assert "gateway_internal_url" not in await _config(db, seeded.tenant_a_id)

    await _upsert(db, seeded.tenant_a_id, gateway_internal_url=_GW)
    assert (await _config(db, seeded.tenant_a_id))["gateway_internal_url"] == _GW


async def test_operator_backfilled_keycloak_creds_survive_a_repoll(db, seeded):
    # Keycloak service creds are still pasted in out-of-band; the poll must not
    # be the thing that drops them.
    await _upsert(db, seeded.tenant_a_id, gateway_internal_url=_GW)
    row = (
        await db.execute(
            select(Integration).where(Integration.tenant_id == seeded.tenant_a_id)
        )
    ).scalar_one()
    row.config = {**row.config, "keycloak_client_id": "daalu-hub-nvcm"}
    await db.commit()

    await _upsert(db, seeded.tenant_a_id, gateway_internal_url=_GW)
    cfg = await _config(db, seeded.tenant_a_id)
    assert cfg["keycloak_client_id"] == "daalu-hub-nvcm"
    assert cfg["gateway_internal_url"] == _GW
