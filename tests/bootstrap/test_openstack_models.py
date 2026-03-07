# tests/bootstrap/test_openstack_models.py
# Tests for daalu.bootstrap.openstack.models.parse_openstack_flag

import pytest
from daalu.bootstrap.openstack.models import parse_openstack_flag, OpenStackSelection


# ---------------------------------------------------------------------------
# parse_openstack_flag
# ---------------------------------------------------------------------------

def test_none_input_returns_all_components():
    sel = parse_openstack_flag(None)
    assert sel.components is None


def test_empty_string_returns_all_components():
    sel = parse_openstack_flag("")
    assert sel.components is None


def test_all_keyword_returns_none():
    sel = parse_openstack_flag("all")
    assert sel.components is None


def test_all_keyword_case_insensitive():
    sel = parse_openstack_flag("ALL")
    assert sel.components is None


def test_single_component():
    sel = parse_openstack_flag("keystone")
    assert sel.components == {"keystone"}


def test_multiple_components_comma_separated():
    sel = parse_openstack_flag("keystone,neutron,glance")
    assert sel.components == {"keystone", "neutron", "glance"}


def test_strips_whitespace_around_items():
    sel = parse_openstack_flag(" keystone , neutron ")
    assert sel.components == {"keystone", "neutron"}


def test_lowercases_components():
    sel = parse_openstack_flag("Keystone,NEUTRON")
    assert sel.components == {"keystone", "neutron"}


def test_ignores_empty_tokens():
    sel = parse_openstack_flag("keystone,,neutron,")
    assert sel.components == {"keystone", "neutron"}


def test_all_in_list_returns_none():
    sel = parse_openstack_flag("keystone,all,neutron")
    assert sel.components is None


def test_component_with_hyphen():
    sel = parse_openstack_flag("kube-coredns-patch")
    assert sel.components == {"kube-coredns-patch"}


def test_openstack_selection_dataclass_default():
    sel = OpenStackSelection()
    assert sel.components is None


def test_openstack_selection_with_explicit_set():
    sel = OpenStackSelection(components={"nova", "cinder"})
    assert "nova" in sel.components
    assert "cinder" in sel.components
