"""Resolving the tenant gateway's in-cluster address.

The hub tool proxy can't reach a tenant's tool UIs without this address, and
``values.compute_urls`` can't derive it: Envoy Gateway names the proxy Service
``envoy-<gw-ns>-<gw-name>-<hash>``, so it has to be looked up by the
owning-gateway label rather than constructed. Before this was read back from
the cluster, every fresh provision left ``gateway_internal_url`` unset and the
proxy failed with "in-cluster gateway address not provisioned".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from daalu_automation.config_manager_controller import progress


def _svc(name: str, ports: list[int]):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(ports=[SimpleNamespace(port=p) for p in ports]),
    )


@pytest.fixture
def fake_core(monkeypatch):
    """Stub the kube client; capture the label selector the lookup used."""
    calls: dict[str, object] = {}

    class _Core:
        def __init__(self, _api):
            pass

        async def list_namespaced_service(self, ns, *, label_selector, **_kw):
            calls["namespace"] = ns
            calls["label_selector"] = label_selector
            result = calls["result"]
            if isinstance(result, Exception):
                raise result
            return SimpleNamespace(items=result)

    class _ApiClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    async def _noop_config(*_a, **_k):
        return None

    monkeypatch.setattr(progress, "load_local_config", _noop_config)
    monkeypatch.setattr(progress, "load_kubeconfig_dict", _noop_config)
    monkeypatch.setattr(
        progress.k8s_client, "ApiClient", lambda *a, **k: _ApiClient()
    )
    monkeypatch.setattr(progress.k8s_client, "CoreV1Api", _Core)
    return calls


async def test_returns_svc_dns_name_not_cluster_ip(fake_core):
    # The name survives a gateway re-provision; the ClusterIP does not.
    fake_core["result"] = [_svc("envoy-cm-default-nv-cm-gateway-c299a3d2", [80])]
    url = await progress.gateway_internal_url("cm-default", None)
    assert url == (
        "http://envoy-cm-default-nv-cm-gateway-c299a3d2"
        ".envoy-gateway-system.svc.cluster.local"
    )


async def test_looks_up_by_owning_gateway_namespace(fake_core):
    fake_core["result"] = [_svc("envoy-cm-acme-gw-abc", [80])]
    await progress.gateway_internal_url("cm-acme", None)
    assert fake_core["namespace"] == "envoy-gateway-system"
    assert fake_core["label_selector"] == (
        "gateway.envoyproxy.io/owning-gateway-namespace=cm-acme"
    )


async def test_port_80_is_left_implicit(fake_core):
    # Envoy exposes 80 first even when other listeners exist; a bare host is
    # what the edge proxy allowlist and the gateway's host routing expect.
    fake_core["result"] = [_svc("envoy-cm-default-gw-x", [8080, 80])]
    url = await progress.gateway_internal_url("cm-default", None)
    assert url.endswith("svc.cluster.local")


async def test_non_standard_port_is_included(fake_core):
    fake_core["result"] = [_svc("envoy-cm-default-gw-x", [8443])]
    url = await progress.gateway_internal_url("cm-default", None)
    assert url.endswith(":8443")


async def test_no_gateway_yet_returns_none(fake_core):
    # Normal mid-install state — the namespace exists before Envoy Gateway
    # has provisioned the proxy Service.
    fake_core["result"] = []
    assert await progress.gateway_internal_url("cm-default", None) is None


async def test_service_without_ports_is_skipped(fake_core):
    fake_core["result"] = [_svc("half-created", [])]
    assert await progress.gateway_internal_url("cm-default", None) is None


async def test_unreachable_cluster_returns_none_rather_than_raising(fake_core):
    # This feeds the progress endpoint, which must not 500 mid-install.
    fake_core["result"] = RuntimeError("connection refused")
    assert await progress.gateway_internal_url("cm-default", None) is None
