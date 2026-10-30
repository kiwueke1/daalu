"""Live network-fabric topology — the interactive graph in the Daalu cockpit.

Renders the tenant's switch/server fabric as nodes + coloured links:
inventory from the source of truth, adjacency from observed LLDP, health from
device telemetry. Read-only and tenant-scoped server-side; a node's SSH
terminal is a separate, admin-gated WebSocket (see ``api/device_terminal.py``).

Gated behind ``tenant.feature_flags["network_topology_enabled"]`` — like the
rest of the network plane it's opt-in per tenant.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id, current_user
from daalu_automation.core.sot.nautobot import NautobotSoT
from daalu_automation.core.topology import TopologyGraph, build_topology
from daalu_automation.core.topology.lldp import collect_observations
from daalu_automation.database import get_db
from daalu_automation.models.tenant import Tenant
from daalu_automation.models.user import User

router = APIRouter(prefix="/topology", tags=["topology"])

TOPOLOGY_FLAG = "network_topology_enabled"


async def _require_topology(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    """403 unless the tenant has the live-topology feature enabled."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    flags = tenant.feature_flags or {}
    if not flags.get(TOPOLOGY_FLAG):
        raise HTTPException(
            status_code=403,
            detail=(
                "live topology is not enabled for this tenant "
                f"(feature_flags.{TOPOLOGY_FLAG})"
            ),
        )
    return tenant


ENV_QUERY = Query(
    None,
    description=(
        "NV-CM management environment slug. Each environment bundles its own "
        "Nautobot and so has its own fabric; omit for the tenant default."
    ),
)


@router.get("", response_model=TopologyGraph)
async def get_topology(
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
    _user: User = Depends(current_user),
) -> TopologyGraph:
    """The whole fabric — nodes, links, and live health. Safe to poll (~10s)."""
    await _require_topology(db, tenant_id)
    return await build_topology(db, tenant_id, env)


@router.get("/nodes/{device_id}")
async def get_node(
    device_id: str,
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
    _user: User = Depends(current_user),
) -> dict:
    """Detail for one node: SoT facts + the neighbours it currently sees.

    Feeds the click-through drawer. Neighbour data comes from the same cached
    LLDP sweep the graph uses, so opening the drawer costs no extra SSH.
    """
    await _require_topology(db, tenant_id)
    sot = NautobotSoT(env=env)
    device = await sot.get_device(db, tenant_id, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")

    observations = await collect_observations(db, tenant_id, [device])
    obs = observations.get(device_id)
    neighbors = (
        [
            {
                "local_if": n.local_if,
                "remote_name": n.remote_name,
                "remote_if": n.remote_if,
            }
            for n in obs.neighbors
        ]
        if obs
        else []
    )
    return {
        "id": device.id,
        "name": device.name,
        "platform": device.platform,
        "transport": device.transport,
        "primary_ip": device.primary_ip,
        "tags": device.tags,
        "reachable": (obs.reachable if obs else None),
        "probe_error": (obs.error if obs else None),
        "neighbors": neighbors,
        # The terminal only makes sense for SSH-shaped transports with an IP.
        "can_terminal": device.transport in {"linux_ssh", "eos", "junos", "iosxr"}
        and bool(device.primary_ip),
    }
