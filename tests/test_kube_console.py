"""Unit tests for the read-only kube console.

No DB, no live cluster — these cover the pure pieces that decide what a
command resolves to and how output is rendered, plus the input
validation that keeps the surface read-only and injection-free.
"""

from __future__ import annotations

import pytest

from daalu_automation.core import kube_console as kc


def test_catalog_ids_unique_and_paths_present():
    ids = [c.id for c in kc.catalog()]
    assert len(ids) == len(set(ids)), "duplicate command ids"
    for c in kc.catalog():
        assert c.path.startswith("/"), c.id
        if c.namespaced:
            assert c.namespaced_path and "{ns}" in c.namespaced_path, c.id


def test_no_secrets_in_catalog():
    # Secrets are deliberately excluded — even a name listing leaks too
    # much for an operator console.
    assert not any("secret" in c.id for c in kc.catalog())
    assert not any("secret" in c.path for c in kc.catalog())


def test_resolve_path_namespaced_vs_all():
    pods = kc._CATALOG_BY_ID["get-pods"]
    assert kc._resolve_path(pods, "kube-system") == (
        "/api/v1/namespaces/kube-system/pods"
    )
    # No namespace → all-namespaces collection.
    assert kc._resolve_path(pods, None) == "/api/v1/pods"
    # Cluster-scoped resource ignores namespace.
    nodes = kc._CATALOG_BY_ID["get-nodes"]
    assert kc._resolve_path(nodes, "anything") == "/api/v1/nodes"


def test_display_command_reflects_flags():
    pods = kc._CATALOG_BY_ID["get-pods"]
    assert kc._display_command(pods, "default", "app=web", "json") == (
        "kubectl get pods -o wide -n default -l app=web -o json"
    )
    assert kc._display_command(pods, None, None, "cli") == (
        "kubectl get pods -o wide -A"
    )


def test_render_table_aligns_columns():
    table = {
        "kind": "Table",
        "columnDefinitions": [{"name": "NAME"}, {"name": "STATUS"}],
        "rows": [
            {"cells": ["pod-a", "Running"]},
            {"cells": ["pod-bbbb", "Pending"]},
        ],
    }
    out = kc._render_table(table)
    lines = out.splitlines()
    assert lines[0].startswith("NAME")
    assert "pod-bbbb" in lines[2]
    # Column 2 header should line up past the widest name.
    assert lines[0].index("STATUS") == lines[1].index("Running")


def test_render_table_empty():
    table = {"kind": "Table", "columnDefinitions": [{"name": "NAME"}], "rows": []}
    assert "(no resources found)" in kc._render_table(table)


def test_raw_get_parses_raw_body_without_deserialization_kwarg():
    # Guards against the kubernetes-client signature drift: _raw_get must
    # NOT pass response_type / response_types_map (renamed across majors),
    # and must parse the raw response body itself.
    captured = {}

    class _Resp:
        data = b'{"kind": "Table", "rows": []}'

    class _FakeApiClient:
        def call_api(self, path, method, **kwargs):
            captured["path"] = path
            captured["method"] = method
            captured["kwargs"] = kwargs
            return _Resp()

    out = kc._raw_get(
        _FakeApiClient(), "/api/v1/nodes", accept="application/json",
        query=[("labelSelector", "a=b")],
    )
    assert out == {"kind": "Table", "rows": []}
    assert captured["method"] == "GET"
    assert captured["kwargs"]["_preload_content"] is False
    assert "response_type" not in captured["kwargs"]
    assert "response_types_map" not in captured["kwargs"]
    assert captured["kwargs"]["header_params"] == {"Accept": "application/json"}
    assert captured["kwargs"]["query_params"] == [("labelSelector", "a=b")]


@pytest.mark.asyncio
async def test_run_commands_rejects_bad_input():
    # These all short-circuit before any cluster I/O, so a dummy cluster
    # stand-in is never touched.
    class _Cluster:
        slug = "x"
        tenant_id = None

    with pytest.raises(kc.KubeConsoleError):
        await kc.run_commands(
            None, _Cluster(), command_ids=[], namespace=None,
            selector=None, output="cli",
        )
    with pytest.raises(kc.KubeConsoleError):
        await kc.run_commands(
            None, _Cluster(), command_ids=["get-pods"],
            namespace="Bad NS!", selector=None, output="cli",
        )
    with pytest.raises(kc.KubeConsoleError):
        await kc.run_commands(
            None, _Cluster(), command_ids=["does-not-exist"],
            namespace=None, selector=None, output="cli",
        )
    with pytest.raises(kc.KubeConsoleError):
        await kc.run_commands(
            None, _Cluster(), command_ids=["get-pods"], namespace=None,
            selector=None, output="xml",  # type: ignore[arg-type]
        )
