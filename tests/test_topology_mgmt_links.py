"""Mgmt-port adjacency classification for the topology graph."""

from daalu_automation.core.topology.service import _is_mgmt_edge


def test_mgmt_edge_both_ends_management():
    assert _is_mgmt_edge("eth0", "eth0")
    assert _is_mgmt_edge("mgmt0", "eth0")
    assert _is_mgmt_edge("Management1", "ma1")
    assert _is_mgmt_edge("fxp0", "em0")


def test_fabric_edge_is_not_mgmt():
    # One fabric end is enough to keep it a real (mis)cable candidate.
    assert not _is_mgmt_edge("swp1", "eth0")
    assert not _is_mgmt_edge("eth1", "eth0")
    assert not _is_mgmt_edge("swp11", "swp2")
    # eth10 is a data port — the eth0 match must not prefix-match it.
    assert not _is_mgmt_edge("eth10", "eth0")


def test_unknown_interfaces_are_not_mgmt():
    assert not _is_mgmt_edge(None, "eth0")
    assert not _is_mgmt_edge("", "")
