# tests/bootstrap/test_infra_component.py
# Tests for daalu.bootstrap.engine.component.InfraComponent

from __future__ import annotations

import pytest
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from daalu.bootstrap.engine.component import InfraComponent


# ---------------------------------------------------------------------------
# Minimal concrete subclass for testing
# ---------------------------------------------------------------------------

@dataclass
class _SimpleComponent(InfraComponent):
    """Concrete minimal component for testing InfraComponent hooks."""
    pass


def _make(
    *,
    tmp_path: Path,
    istio_enabled: bool = False,
    istio_host: Optional[str] = None,
    istio_service: Optional[str] = None,
    istio_service_namespace: Optional[str] = None,
    istio_service_port: Optional[int] = None,
    uses_helm: bool = True,
) -> _SimpleComponent:
    return _SimpleComponent(
        name="test-comp",
        repo_name="local",
        repo_url="",
        chart="test/chart",
        version=None,
        namespace="openstack",
        release_name="test-comp",
        local_chart_dir=tmp_path,
        remote_chart_dir=tmp_path,
        kubeconfig="/tmp/fake.yaml",
        uses_helm=uses_helm,
        istio_enabled=istio_enabled,
        istio_host=istio_host,
        istio_service=istio_service,
        istio_service_namespace=istio_service_namespace,
        istio_service_port=istio_service_port,
    )


# ---------------------------------------------------------------------------
# Fake kubectl for virtualservice tests
# ---------------------------------------------------------------------------

class FakeKubectl:
    def __init__(self, vs_exists: bool = False):
        self._vs_exists = vs_exists
        self.applied: list[list] = []

    def resource_exists(self, *, kind, name, namespace):
        return self._vs_exists

    def apply_objects(self, objs: list) -> None:
        self.applied.append(objs)


# ---------------------------------------------------------------------------
# load_values_file
# ---------------------------------------------------------------------------

def test_load_values_file_returns_dict(tmp_path):
    values = tmp_path / "values.yaml"
    values.write_text("key: value\nnested:\n  x: 1\n")
    comp = _make(tmp_path=tmp_path)
    data = comp.load_values_file(values)
    assert data == {"key": "value", "nested": {"x": 1}}


def test_load_values_file_returns_empty_dict_for_empty_yaml(tmp_path):
    values = tmp_path / "values.yaml"
    values.write_text("")
    comp = _make(tmp_path=tmp_path)
    data = comp.load_values_file(values)
    assert data == {}


def test_load_values_file_raises_when_file_missing(tmp_path):
    comp = _make(tmp_path=tmp_path)
    with pytest.raises(FileNotFoundError):
        comp.load_values_file(tmp_path / "nonexistent.yaml")


# ---------------------------------------------------------------------------
# _ensure_virtualservice
# ---------------------------------------------------------------------------

def test_ensure_virtualservice_skips_when_istio_disabled(tmp_path):
    kubectl = FakeKubectl()
    comp = _make(tmp_path=tmp_path, istio_enabled=False)
    comp._ensure_virtualservice(kubectl)
    assert kubectl.applied == []


def test_ensure_virtualservice_skips_when_already_exists(tmp_path):
    kubectl = FakeKubectl(vs_exists=True)
    comp = _make(
        tmp_path=tmp_path,
        istio_enabled=True,
        istio_host="svc.daalu.io",
        istio_service="svc",
        istio_service_namespace="openstack",
        istio_service_port=8080,
    )
    comp._ensure_virtualservice(kubectl)
    assert kubectl.applied == []


def test_ensure_virtualservice_creates_manifest_when_not_exists(tmp_path):
    kubectl = FakeKubectl(vs_exists=False)
    comp = _make(
        tmp_path=tmp_path,
        istio_enabled=True,
        istio_host="identity.daalu.io",
        istio_service="keystone-api",
        istio_service_namespace="openstack",
        istio_service_port=5000,
    )
    comp._ensure_virtualservice(kubectl)
    assert len(kubectl.applied) == 1
    manifest = kubectl.applied[0][0]
    assert manifest["kind"] == "VirtualService"
    assert manifest["metadata"]["name"] == "test-comp-vs"
    assert manifest["spec"]["hosts"] == ["identity.daalu.io"]
    route = manifest["spec"]["http"][0]["route"][0]["destination"]
    assert "keystone-api.openstack.svc.cluster.local" == route["host"]
    assert route["port"]["number"] == 5000


def test_ensure_virtualservice_raises_when_fields_incomplete(tmp_path):
    kubectl = FakeKubectl(vs_exists=False)
    comp = _make(
        tmp_path=tmp_path,
        istio_enabled=True,
        istio_host="svc.daalu.io",
        # Missing istio_service, istio_service_namespace, istio_service_port
    )
    with pytest.raises(RuntimeError, match="not fully defined"):
        comp._ensure_virtualservice(kubectl)


# ---------------------------------------------------------------------------
# post_install (default hook behaviour)
# ---------------------------------------------------------------------------

def test_post_install_calls_ensure_virtualservice(tmp_path):
    kubectl = FakeKubectl(vs_exists=False)
    comp = _make(
        tmp_path=tmp_path,
        istio_enabled=True,
        istio_host="horizon.daalu.io",
        istio_service="horizon",
        istio_service_namespace="openstack",
        istio_service_port=80,
    )
    comp.post_install(kubectl)
    # VirtualService was applied
    assert len(kubectl.applied) == 1


def test_post_install_noop_when_istio_disabled(tmp_path):
    kubectl = FakeKubectl()
    comp = _make(tmp_path=tmp_path, istio_enabled=False)
    comp.post_install(kubectl)
    assert kubectl.applied == []


# ---------------------------------------------------------------------------
# values() default
# ---------------------------------------------------------------------------

def test_values_returns_empty_dict_by_default(tmp_path):
    comp = _make(tmp_path=tmp_path)
    assert comp.values() == {}
