# tests/bootstrap/test_neutron.py
# Tests for daalu.bootstrap.openstack.components.neutron.neutron.NeutronComponent

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from daalu.bootstrap.openstack.components.neutron.neutron import NeutronComponent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_component(tmp_path: Path, **kwargs) -> NeutronComponent:
    """Return a NeutronComponent with minimal real filesystem setup."""
    values_file = tmp_path / "values.yaml"
    values_file.write_text("# empty\n")
    defaults = dict(
        values_path=values_file,
        assets_dir=tmp_path,
        kubeconfig="/tmp/fake.kubeconfig",
        secrets_path=tmp_path / "secrets.yaml",
        keystone_public_host="identity.daalu.io",
        network_public_host="network.daalu.io",
        network_backend="ovn",
    )
    defaults.update(kwargs)
    return NeutronComponent(**defaults)


def _inject_endpoints(comp: NeutronComponent, metadata_secret: str = "") -> None:
    """Inject pre-computed attributes that pre_install would normally set."""
    comp._computed_endpoints = {
        "identity": {
            "auth": {
                "admin": {
                    "region_name": "RegionOne",
                    "username": "admin-RegionOne",
                    "password": "adminpw",
                    "project_name": "admin",
                    "user_domain_name": "default",
                    "project_domain_name": "default",
                    "default_domain_id": "default",
                }
            },
            "hosts": {"default": "keystone-api"},
            "port": {"api": {"default": 5000}},
        },
        "oslo_db": {},
        "oslo_messaging": {},
        "oslo_cache": {},
    }
    comp._neutron_keystone_password = "neutron_ks_pw"
    comp._nova_keystone_password = "nova_ks_pw"
    comp._metadata_secret = metadata_secret


# ---------------------------------------------------------------------------
# values() — common behaviour
# ---------------------------------------------------------------------------

def test_values_raises_if_endpoints_not_computed(tmp_path):
    comp = _make_component(tmp_path)
    with pytest.raises(RuntimeError, match="endpoints not computed"):
        comp.values()


def test_values_sets_network_backend_in_network_key(tmp_path):
    comp = _make_component(tmp_path, network_backend="ovs")
    _inject_endpoints(comp)
    result = comp.values()
    assert result["network"]["backend"] == ["ovs"]


def test_values_sets_ovn_backend(tmp_path):
    comp = _make_component(tmp_path, network_backend="ovn")
    _inject_endpoints(comp)
    result = comp.values()
    assert result["network"]["backend"] == ["ovn"]


def test_values_injects_neutron_service_user_in_identity(tmp_path):
    comp = _make_component(tmp_path)
    _inject_endpoints(comp)
    result = comp.values()
    neutron_auth = result["endpoints"]["identity"]["auth"]["neutron"]
    assert neutron_auth["username"] == "neutron"
    assert neutron_auth["password"] == "neutron_ks_pw"
    assert "admin" in neutron_auth["role"]


def test_values_injects_nova_auth_for_notifications(tmp_path):
    comp = _make_component(tmp_path)
    _inject_endpoints(comp)
    result = comp.values()
    nova_auth = result["endpoints"]["identity"]["auth"]["nova"]
    assert nova_auth["username"] == "nova"
    assert nova_auth["password"] == "nova_ks_pw"


def test_values_sets_network_public_endpoint(tmp_path):
    comp = _make_component(tmp_path, network_public_host="network.daalu.io")
    _inject_endpoints(comp)
    result = comp.values()
    net_ep = result["endpoints"]["network"]
    assert net_ep["host_fqdn_override"]["public"]["host"] == "network.daalu.io"
    assert net_ep["scheme"]["public"] == "https"
    assert net_ep["port"]["api"]["public"] == 443


def test_values_injects_metadata_secret_in_metadata_agent(tmp_path):
    comp = _make_component(tmp_path)
    _inject_endpoints(comp, metadata_secret="shared_secret_pw")
    result = comp.values()
    secret = result["conf"]["metadata_agent"]["DEFAULT"]["metadata_proxy_shared_secret"]
    assert secret == "shared_secret_pw"


def test_values_skips_metadata_agent_when_secret_empty(tmp_path):
    comp = _make_component(tmp_path)
    _inject_endpoints(comp, metadata_secret="")
    result = comp.values()
    # metadata_agent section must not be set
    assert "metadata_agent" not in result.get("conf", {})


# ---------------------------------------------------------------------------
# _apply_ovn_overrides()
# ---------------------------------------------------------------------------

def test_apply_ovn_overrides_sets_service_plugins(tmp_path):
    comp = _make_component(tmp_path)
    _inject_endpoints(comp, metadata_secret="pw")
    base = comp.values()
    # service_plugins is injected by _apply_ovn_overrides via values()
    plugins = base["conf"]["neutron"]["DEFAULT"]["service_plugins"]
    assert "ovn-router" in plugins
    assert "trunk" in plugins


def test_apply_ovn_overrides_injects_ovn_metadata_secret_when_present(tmp_path):
    comp = _make_component(tmp_path)
    _inject_endpoints(comp, metadata_secret="ovn_secret_pw")
    result = comp.values()
    secret = result["conf"]["ovn_metadata_agent"]["DEFAULT"]["metadata_proxy_shared_secret"]
    assert secret == "ovn_secret_pw"


def test_apply_ovn_overrides_skips_ovn_metadata_agent_when_empty_secret(tmp_path):
    comp = _make_component(tmp_path)
    _inject_endpoints(comp, metadata_secret="")
    result = comp.values()
    assert "ovn_metadata_agent" not in result.get("conf", {})


def test_apply_ovn_overrides_disables_non_ovn_manifests(tmp_path):
    comp = _make_component(tmp_path)
    _inject_endpoints(comp)
    result = comp.values()
    manifests = result["manifests"]
    assert manifests["daemonset_dhcp_agent"] is False
    assert manifests["daemonset_l3_agent"] is False
    assert manifests["daemonset_ovs_agent"] is False


def test_apply_ovn_overrides_enables_ovn_manifests(tmp_path):
    comp = _make_component(tmp_path)
    _inject_endpoints(comp)
    result = comp.values()
    manifests = result["manifests"]
    assert manifests["daemonset_ovn_metadata_agent"] is True
    assert manifests["daemonset_ovn_vpn_agent"] is True


def test_apply_ovn_overrides_sets_ml2_type_drivers(tmp_path):
    comp = _make_component(tmp_path)
    _inject_endpoints(comp)
    result = comp.values()
    ml2 = result["conf"]["plugins"]["ml2_conf"]["ml2"]
    assert "geneve" in ml2["type_drivers"]
    assert "flat" in ml2["type_drivers"]


def test_ovn_overrides_not_applied_for_ovs_backend(tmp_path):
    """When backend is ovs, OVN manifests should not be set."""
    comp = _make_component(tmp_path, network_backend="ovs")
    _inject_endpoints(comp)
    result = comp.values()
    assert "manifests" not in result


# ---------------------------------------------------------------------------
# _cleanup_stale_jobs
# ---------------------------------------------------------------------------

def test_cleanup_stale_jobs_deletes_when_job_exists(tmp_path):
    deleted = []

    class FakeKubectl:
        def _run(self, cmd):
            if "get job" in cmd:
                return (0, "job/neutron-db-sync", "")
            if "delete job" in cmd:
                deleted.append(cmd)
                return (0, "", "")
            return (0, "", "")

    comp = _make_component(tmp_path)
    comp._cleanup_stale_jobs(FakeKubectl())
    assert any("neutron-db-sync" in d for d in deleted)
    assert any("neutron-rabbit-init" in d for d in deleted)


def test_cleanup_stale_jobs_skips_when_not_found(tmp_path):
    deleted = []

    class FakeKubectl:
        def _run(self, cmd):
            return (1, "", "not found")  # rc=1 means job doesn't exist

    comp = _make_component(tmp_path)
    comp._cleanup_stale_jobs(FakeKubectl())
    assert deleted == []
