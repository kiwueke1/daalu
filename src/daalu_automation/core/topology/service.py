"""Compose the live topology graph from inventory + observed LLDP + telemetry.

``build_topology`` is the one entry point the router calls. It:

1. seeds **nodes** from the source of truth (Nautobot devices),
2. reads the **intended cabling** (Nautobot) — the baseline that lets a missing
   link be called *down* rather than merely *unseen*,
3. sweeps **observed LLDP** neighbours (``core.topology.lldp``) — the live wire,
4. reconciles the two into coloured **links**, and folds in device reachability
   as a telemetry heartbeat.

Every external read is best-effort: with Nautobot down you get an empty graph
with a ``degraded`` reason; with devices unreachable you get the intended
inventory drawn neutral. It never renders a probe failure as a red link.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import datetime, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.sot.models import Device
from daalu_automation.core.sot.nautobot import NautobotSoT, NautobotUnavailable
from daalu_automation.core.topology.lldp import NodeObservation, collect_observations
from daalu_automation.core.topology.models import (
    NodeTier,
    TopologyGraph,
    TopologyLink,
    TopologyNode,
)

logger = structlog.get_logger(__name__)

# Transports that expose an interactive SSH shell for the in-browser terminal.
_TERMINAL_TRANSPORTS = {"linux_ssh", "eos", "junos", "iosxr"}

# Interface names that are out-of-band management ports across the vendors we
# meet: eth0/mgmt0 (Cumulus, SONiC, generic Linux), ma1 (SONiC), fxp0/em0
# (Junos), Management0/1 (EOS/NX-OS). An observed adjacency where BOTH ends
# are management ports is the shared OOB network, not fabric cabling.
_MGMT_IF_RE = re.compile(r"^(eth0|mgmt\d*|ma1|fxp0|em0|management\d*)$", re.IGNORECASE)


def _is_mgmt_edge(a_if: str | None, b_if: str | None) -> bool:
    return bool(a_if and b_if and _MGMT_IF_RE.match(a_if) and _MGMT_IF_RE.match(b_if))


def _tier_for(device: Device) -> NodeTier:
    """Place a device in the fabric by role/tags/name/platform (best-effort)."""
    hay = " ".join(
        [
            device.name or "",
            " ".join(device.tags or []),
            str((device.extra or {}).get("role", "")),
            str((device.extra or {}).get("daalu_tier", "")),
        ]
    ).lower()
    if "spine" in hay or "super-spine" in hay or "superspine" in hay:
        return "spine"
    if any(k in hay for k in ("leaf", "rail", "tor", "top-of-rack")):
        return "leaf"
    if any(k in hay for k in ("border", "gateway", "exit", "edge")):
        return "gateway"
    if device.platform == "linux" or any(
        k in hay for k in ("server", "host", "gpu", "compute", "node")
    ):
        return "server"
    return "unknown"


def _to_node(device: Device) -> TopologyNode:
    return TopologyNode(
        id=device.id,
        name=device.name or device.id,
        tier=_tier_for(device),
        platform=device.platform or "",
        transport=device.transport or "",
        primary_ip=device.primary_ip,
        tags=list(device.tags or []),
        can_terminal=(device.transport in _TERMINAL_TRANSPORTS) and bool(device.primary_ip),
    )


def _endpoint_lt(a: tuple[str, str], b: tuple[str, str]) -> bool:
    return a <= b


def _canonical(
    a_id: str, a_if: str | None, b_id: str, b_if: str | None
) -> tuple[tuple[str, str], tuple[str, str]]:
    """Order the two endpoints so an undirected link has one stable id."""
    left = (a_id, a_if or "")
    right = (b_id, b_if or "")
    return (left, right) if _endpoint_lt(left, right) else (right, left)


def _link_id(left: tuple[str, str], right: tuple[str, str]) -> str:
    return f"{left[0]}:{left[1]}--{right[0]}:{right[1]}"


class _Edge:
    """Mutable accumulator while reconciling; frozen into a TopologyLink at the end."""

    __slots__ = ("left", "right", "intended", "obs_left", "obs_right")

    def __init__(self, left: tuple[str, str], right: tuple[str, str]):
        self.left = left
        self.right = right
        self.intended = False
        self.obs_left = False  # LLDP from the 'left' endpoint saw 'right'
        self.obs_right = False


async def build_topology(
    db: AsyncSession, tenant_id: uuid.UUID, env: str | None = None
) -> TopologyGraph:
    """The reconciled fabric graph for one management environment.

    ``env`` is an NV-CM environment slug. Each environment bundles its own
    Nautobot and therefore its own fabric, so the slug decides which set of
    devices and cabling this graph describes; ``None`` falls back to the
    tenant's default source of truth.
    """
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    sot = NautobotSoT(env=env)

    # 1. Inventory ----------------------------------------------------------
    try:
        devices = await sot.list_devices(db, tenant_id)
    except NautobotUnavailable as e:
        return TopologyGraph(
            generated_at=now_iso,
            observed=False,
            degraded=f"source of truth unavailable: {e}",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("topology.inventory_failed", error=str(e))
        return TopologyGraph(
            generated_at=now_iso, observed=False, degraded=f"inventory error: {e}"
        )

    nodes = [_to_node(d) for d in devices]
    id_by_name = {d.name: d.id for d in devices if d.name}
    node_ids = {d.id for d in devices}

    # 2. Intended cabling (best-effort) ------------------------------------
    cabling = await sot.list_cabling(db, tenant_id)
    have_cabling = bool(cabling)

    # 3. Observed LLDP ------------------------------------------------------
    observations: dict[str, NodeObservation] = await collect_observations(
        db, tenant_id, devices
    )
    probed = {oid for oid, o in observations.items()}
    reachable = {oid for oid, o in observations.items() if o.reachable}

    # 4. Reconcile ----------------------------------------------------------
    edges: dict[str, _Edge] = {}

    def _edge(a_id: str, a_if: str | None, b_id: str, b_if: str | None) -> _Edge:
        left, right = _canonical(a_id, a_if, b_id, b_if)
        eid = _link_id(left, right)
        e = edges.get(eid)
        if e is None:
            e = _Edge(left, right)
            edges[eid] = e
        return e

    # 4a. intended edges from cabling
    for dn, lif, rdn, rif in cabling:
        a_id, b_id = id_by_name.get(dn), id_by_name.get(rdn)
        if not a_id or not b_id:
            continue
        _edge(a_id, lif, b_id, rif).intended = True

    # 4b. observed edges from LLDP.
    #
    # Two reconciliation rules keep observations honest:
    #
    # * **Shared segments stay off fabric ports.** More than one LLDP
    #   neighbour on a port means a shared L2 segment. On a mgmt-shaped
    #   interface that's the OOB network — keep every adjacency (the mgmt
    #   mesh renders in its own link class). On a fabric port it's a rogue
    #   hub / bridge, and drawing any single pair fabricates a p2p cable
    #   that doesn't exist — skip those observations.
    # * **The remote interface spelling is advisory.** The observing device
    #   names its own port authoritatively, but the remote port comes from
    #   the neighbour's PortID TLV, which plain-Linux lldpd emits as a MAC
    #   address rather than the ifname the cabling plan uses. Requiring an
    #   exact remote_if match would split one physical cable into two graph
    #   edges — a spurious "mismatch" next to a false "down". An observation
    #   first tries to land on the intended edge sharing its (device,
    #   local_if) endpoint and remote *device*; only failing that does it
    #   create a new edge keyed on the observed endpoints verbatim.
    intended_by_endpoint: dict[tuple[str, str], _Edge] = {}
    for e in edges.values():
        if e.intended:
            intended_by_endpoint[e.left] = e
            intended_by_endpoint[e.right] = e

    for oid, obs in observations.items():
        if oid not in node_ids:
            continue
        neighbors_per_if = Counter(nb.local_if or "" for nb in obs.neighbors)
        for nb in obs.neighbors:
            r_id = id_by_name.get(nb.remote_name)
            if not r_id or r_id == oid:
                continue
            local_if = nb.local_if or ""
            if neighbors_per_if[local_if] > 1 and not _MGMT_IF_RE.match(local_if):
                continue  # shared segment on a fabric port — see above
            local = (oid, local_if)
            e = None
            cand = intended_by_endpoint.get(local)
            if cand is not None:
                far = cand.right if cand.left == local else cand.left
                if far[0] == r_id:
                    e = cand
            if e is None:
                e = _edge(oid, nb.local_if, r_id, nb.remote_if)
            # Mark which side reported it (the local device 'oid' is one endpoint).
            if local == e.left:
                e.obs_left = True
            else:
                e.obs_right = True

    # 4c. freeze into coloured links
    links: list[TopologyLink] = []
    for eid, e in edges.items():
        (a_id, a_if), (b_id, b_if) = e.left, e.right
        observed = e.obs_left or e.obs_right
        if observed:
            if _is_mgmt_edge(a_if, b_if) and not e.intended:
                # Every device also sees its peers on the shared out-of-band
                # management network (eth0/mgmt0/…). That adjacency is real
                # but is never part of the fabric cabling plan — calling it
                # "miscabled" (or, with no plan loaded yet, "up") would paint
                # phantom spine↔server cables. An *intended* mgmt-port cable
                # (an OOB switch modeled in the SoT) still colours "up" below.
                status = "mgmt"
                detail = "management network (out-of-band) — expected, not a fabric link"
            elif e.intended or not have_cabling:
                status, detail = "up", ""
            else:
                status = "mismatch"
                detail = "observed on the wire but not in the cabling plan"
        else:
            both_probed = a_id in reachable and b_id in reachable
            if e.intended and both_probed:
                status = "down"
                detail = "cabled in the plan but no LLDP neighbour observed"
            else:
                status = "unknown"
                detail = "not observed (endpoint not reachable / no SSH probe)"
        links.append(
            TopologyLink(
                id=eid,
                source=a_id,
                source_if=a_if or None,
                target=b_id,
                target_if=b_if or None,
                status=status,
                source_observed=e.obs_left,
                target_observed=e.obs_right,
                intended=e.intended,
                detail=detail,
            )
        )

    # 5. Node-level telemetry heartbeat ------------------------------------
    for node in nodes:
        if node.id in probed:
            node.reachable = node.id in reachable
            node.lldp_ok = any(
                (o.neighbors and o.device_id == node.id) for o in observations.values()
            )
        # else: not probed → leave None (UI draws neutral)

    stats = {
        "nodes": len(nodes),
        "links": len(links),
        "up": sum(1 for lk in links if lk.status == "up"),
        "down": sum(1 for lk in links if lk.status == "down"),
        "mismatch": sum(1 for lk in links if lk.status == "mismatch"),
        "mgmt": sum(1 for lk in links if lk.status == "mgmt"),
        "unknown": sum(1 for lk in links if lk.status == "unknown"),
    }

    observed = bool(reachable) or bool(links and any(lk.status == "up" for lk in links))
    degraded: str | None = None
    if not observed:
        if not have_cabling and not observations:
            degraded = "inventory only — no reachable devices and no cabling plan yet"
        elif not observations:
            degraded = "no SSH credentials to probe devices — showing intended cabling"
        else:
            degraded = "no devices reachable — showing intended inventory"

    return TopologyGraph(
        nodes=nodes,
        links=links,
        generated_at=now_iso,
        observed=observed,
        degraded=degraded,
        stats=stats,
    )
