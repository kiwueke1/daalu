"""lldpctl keyvalue parsing — per-neighbour block fidelity.

lldpd repeats the same key path for every neighbour it holds on an
interface (a flooded mgmt bridge shows several). The old parser folded
all blocks on an interface into one dict — last chassis name paired
with first port name — fabricating a neighbour that was never
advertised. The parser must emit every block verbatim; what a
multi-neighbour interface *means* is the service's concern.
"""

from __future__ import annotations

from daalu_automation.core.topology.lldp import _parse_lldpctl_keyvalue

# Trimmed from a real `lldpctl -f keyvalue` on a containerlab Cumulus VX
# leaf: eth0 sits on the flooded docker mgmt bridge (3 neighbours), the
# swp uplinks are genuine p2p fabric links.
_LEAF1_OUTPUT = """\
lldp.eth0.via=LLDP
lldp.eth0.chassis.name=spine1
lldp.eth0.chassis.descr=Cumulus Linux
lldp.eth0.port.ifname=eth0
lldp.eth0.via=LLDP
lldp.eth0.chassis.name=leaf2
lldp.eth0.port.ifname=eth0
lldp.eth0.via=LLDP
lldp.eth0.chassis.name=spine2
lldp.eth0.port.ifname=eth0
lldp.swp11.via=LLDP
lldp.swp11.chassis.name=spine1
lldp.swp11.port.ifname=swp1
lldp.swp12.via=LLDP
lldp.swp12.chassis.name=spine2
lldp.swp12.port.ifname=swp1
"""


def test_every_neighbor_block_is_emitted():
    neighbors = _parse_lldpctl_keyvalue(_LEAF1_OUTPUT)
    eth0 = sorted(n.remote_name for n in neighbors if n.local_if == "eth0")
    # All three mgmt-bridge neighbours survive, each its own entry — the
    # old parse collapsed them into one garbled "spine2 on eth0".
    assert eth0 == ["leaf2", "spine1", "spine2"]
    by_if = {n.local_if: n for n in neighbors if n.local_if != "eth0"}
    assert (by_if["swp11"].remote_name, by_if["swp11"].remote_if) == ("spine1", "swp1")
    assert (by_if["swp12"].remote_name, by_if["swp12"].remote_if) == ("spine2", "swp1")


def test_single_neighbor_parse_unchanged():
    text = (
        "lldp.swp1.chassis.name=gpu1\n"
        "lldp.swp1.chassis.descr=Ubuntu\n"
        "lldp.swp1.port.ifname=eth1\n"
        "lldp.swp1.port.descr=rail0\n"
    )
    (n,) = _parse_lldpctl_keyvalue(text)
    assert (n.local_if, n.remote_name, n.remote_if) == ("swp1", "gpu1", "eth1")


def test_port_field_fallback_order():
    # No ifname → descr wins over mac (within one block).
    text = (
        "lldp.swp2.chassis.name=gpu2\n"
        "lldp.swp2.port.mac=aa:bb:cc:dd:ee:ff\n"
        "lldp.swp2.port.descr=rail1\n"
    )
    (n,) = _parse_lldpctl_keyvalue(text)
    assert n.remote_if == "rail1"


def test_blocks_keep_their_own_port_fields():
    # Two neighbours on one port: names pair with *their* ports, not
    # first-seen ones.
    text = (
        "lldp.eth0.chassis.name=sw1\n"
        "lldp.eth0.port.ifname=Ethernet1\n"
        "lldp.eth0.chassis.name=sw2\n"
        "lldp.eth0.port.ifname=Ethernet9\n"
    )
    pairs = {(n.remote_name, n.remote_if) for n in _parse_lldpctl_keyvalue(text)}
    assert pairs == {("sw1", "Ethernet1"), ("sw2", "Ethernet9")}


def test_garbage_lines_ignored():
    assert _parse_lldpctl_keyvalue("not-lldp\nlldp.short=x\n\n") == []
