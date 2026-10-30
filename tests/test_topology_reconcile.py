"""build_topology reconciliation — observed LLDP vs intended cabling.

Pins two rules:

* **Lenient remote-interface matching** — the observing device names its
  own port authoritatively, but the *remote* port comes from the
  neighbour's PortID TLV, which plain-Linux lldpd emits as a MAC address
  — not the ifname the cabling plan uses. An observation must land on
  the planned cable sharing its (device, local_if) endpoint + remote
  device, not split into a spurious "mismatch"/"down" pair.

* **Shared segments stay off fabric ports** — >1 LLDP neighbour on a
  port is a shared L2 segment. On mgmt-shaped interfaces (eth0/mgmt0/…)
  that's the OOB network and every adjacency renders in the "mgmt"
  class; on a fabric port it's a rogue hub and drawing any single pair
  fabricates a cable, so those observations are skipped.
"""

from __future__ import annotations

import uuid

import pytest

from daalu_automation.core.sot.models import Device
from daalu_automation.core.topology import service as svc
from daalu_automation.core.topology.lldp import Neighbor, NodeObservation

_TENANT = uuid.uuid4()


def _device(name: str, ip: str) -> Device:
    return Device(
        id=f"id-{name}",
        name=name,
        primary_ip=ip,
        platform="linux",
        transport="linux_ssh",
        tags=[],
        extra={},
    )


class _StubSoT:
    def __init__(self, devices, cabling):
        self._devices = devices
        self._cabling = cabling

    async def list_devices(self, db, tenant_id):
        return self._devices

    async def list_cabling(self, db, tenant_id):
        return self._cabling


# Stands in for the NautobotSoT *constructor*, so it has to swallow the
# environment slug build_topology now passes (which Nautobot to read).
def _two_device_sot(env=None):
    return _StubSoT(
        [_device("leaf1", "10.0.0.1"), _device("gpu1", "10.0.0.2")],
        [("leaf1", "swp1", "gpu1", "eth1")],
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_mac_flavored_remote_if_lands_on_planned_cable(monkeypatch):
    """leaf1 observes gpu1 with a MAC PortID → still the planned cable, up."""
    monkeypatch.setattr(svc, "NautobotSoT", _two_device_sot)

    async def _obs(db, tenant_id, devices):
        return {
            "id-leaf1": NodeObservation(
                device_id="id-leaf1",
                reachable=True,
                neighbors=[
                    Neighbor(
                        local_if="swp1",
                        remote_name="gpu1",
                        remote_if="aa:bb:cc:dd:ee:01",  # MAC, not "eth1"
                    )
                ],
            ),
            "id-gpu1": NodeObservation(device_id="id-gpu1", reachable=True),
        }

    monkeypatch.setattr(svc, "collect_observations", _obs)

    graph = await svc.build_topology(None, _TENANT)
    assert len(graph.links) == 1  # one cable, not a mismatch/down pair
    (link,) = graph.links
    assert link.intended is True
    assert link.status == "up"
    assert graph.stats["mismatch"] == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_truly_unplanned_link_still_flags_mismatch(monkeypatch):
    """An observation on a port with no planned cable stays a mismatch."""
    monkeypatch.setattr(svc, "NautobotSoT", _two_device_sot)

    async def _obs(db, tenant_id, devices):
        return {
            "id-leaf1": NodeObservation(
                device_id="id-leaf1",
                reachable=True,
                neighbors=[
                    # Planned cable unobserved; a different port sees gpu1.
                    Neighbor(local_if="swp9", remote_name="gpu1", remote_if="eth2"),
                ],
            ),
            "id-gpu1": NodeObservation(device_id="id-gpu1", reachable=True),
        }

    monkeypatch.setattr(svc, "collect_observations", _obs)

    graph = await svc.build_topology(None, _TENANT)
    by_status = {lk.status for lk in graph.links}
    assert by_status == {"mismatch", "down"}  # rogue wire + missing planned cable


@pytest.mark.asyncio(loop_scope="session")
async def test_shared_segment_on_fabric_port_is_skipped(monkeypatch):
    """Two neighbours on swp1 = a hub/bridge, not a cable — no edge drawn."""
    monkeypatch.setattr(
        svc,
        "NautobotSoT",
        lambda env=None: _StubSoT(
            [
                _device("leaf1", "10.0.0.1"),
                _device("gpu1", "10.0.0.2"),
                _device("gpu2", "10.0.0.3"),
            ],
            [],
        ),
    )

    async def _obs(db, tenant_id, devices):
        return {
            "id-leaf1": NodeObservation(
                device_id="id-leaf1",
                reachable=True,
                neighbors=[
                    Neighbor(local_if="swp1", remote_name="gpu1", remote_if="eth1"),
                    Neighbor(local_if="swp1", remote_name="gpu2", remote_if="eth1"),
                ],
            ),
        }

    monkeypatch.setattr(svc, "collect_observations", _obs)

    graph = await svc.build_topology(None, _TENANT)
    assert graph.links == []


@pytest.mark.asyncio(loop_scope="session")
async def test_mgmt_interface_keeps_its_shared_segment_mesh(monkeypatch):
    """eth0 seeing several peers is the OOB network — every adjacency
    renders, in the mgmt link class."""
    monkeypatch.setattr(
        svc,
        "NautobotSoT",
        lambda env=None: _StubSoT(
            [
                _device("leaf1", "10.0.0.1"),
                _device("spine1", "10.0.0.2"),
                _device("spine2", "10.0.0.3"),
            ],
            [],
        ),
    )

    async def _obs(db, tenant_id, devices):
        return {
            "id-leaf1": NodeObservation(
                device_id="id-leaf1",
                reachable=True,
                neighbors=[
                    Neighbor(local_if="eth0", remote_name="spine1", remote_if="eth0"),
                    Neighbor(local_if="eth0", remote_name="spine2", remote_if="eth0"),
                ],
            ),
        }

    monkeypatch.setattr(svc, "collect_observations", _obs)

    graph = await svc.build_topology(None, _TENANT)
    assert len(graph.links) == 2
    assert {lk.status for lk in graph.links} == {"mgmt"}
    assert graph.stats["mgmt"] == 2
