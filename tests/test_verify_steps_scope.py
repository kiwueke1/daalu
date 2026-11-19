"""The verify tail of an approved fix lists the fixed Deployment's pods only."""

from __future__ import annotations

from types import SimpleNamespace

from daalu_automation.api.routers import alert_chat
from daalu_automation.core import kube_tools


def _action(tool: str, **inp):
    return SimpleNamespace(tool_name=tool, tool_input=inp)


def test_deployment_fix_checks_only_that_deployments_pods():
    steps = alert_chat._verification_steps(
        _action("rollout_undo", namespace="openstack", name="keystone-api", to_revision=3)
    )
    assert [s[0] for s in steps] == ["get_deployment", "list_pods"]
    assert steps[1][1] == {"namespace": "openstack", "deployment": "keystone-api"}
    assert steps[1][2] == "Check keystone-api pods in openstack"


def test_pod_fix_falls_back_to_the_namespace():
    steps = alert_chat._verification_steps(_action("delete_pod", namespace="demo", name="web-1"))
    assert steps == [("list_pods", {"namespace": "demo"}, "Check pods in demo")]


async def test_list_pods_resolves_the_deployment_selector(monkeypatch):
    seen: dict = {}
    dep = SimpleNamespace(spec=SimpleNamespace(selector=SimpleNamespace(
        match_labels={"application": "keystone", "component": "api"})))

    class Apps:
        def read_namespaced_deployment(self, **kw):
            seen["read"] = kw
            return dep

    class Core:
        def list_namespaced_pod(self, **kw):
            seen["list"] = kw
            return SimpleNamespace(items=[])

    async def fake_kube(_tenant_id):
        return Core(), Apps(), None

    monkeypatch.setattr(kube_tools, "_tenant_kube_or_default", fake_kube)
    out = await kube_tools._list_pods(namespace="openstack", deployment="keystone-api")
    assert out.startswith("NAME")
    assert seen["read"]["name"] == "keystone-api"
    assert seen["list"]["label_selector"] == "application=keystone,component=api"


def test_big_tool_output_is_clipped_for_the_model_keeping_the_tail():
    log = "HEAD\n" + "x" * 40_000 + "\nNEWEST LINE"
    out = alert_chat._clip_tool_output(log)
    assert len(out) < 6_200
    assert out.startswith("HEAD") and out.endswith("NEWEST LINE")
    assert "characters cut" in out
    assert alert_chat._clip_tool_output("short") == "short"


def test_unavailable_text_names_the_cause():
    ctx = alert_chat._unavailable_text(
        "All tool-capable tiers failed: Error code: 400 - This model's maximum "
        "context length is 32768 tokens"
    )
    assert "context window" in ctx and "offline" not in ctx
    none = alert_chat._unavailable_text("No tool-capable LLM tier configured. Set LLM_API_KEY")
    assert "offline" in none
    other = alert_chat._unavailable_text("All tool-capable tiers failed: 503")
    assert "didn't answer" in other
