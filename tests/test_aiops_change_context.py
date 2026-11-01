"""Change context (``recent_changes``) and write previews (AOMM §6), against a
mocked kube client — no real cluster."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

NS = SimpleNamespace
NOW = datetime.now(tz=timezone.utc)


def _container(name="dummy", image="nginx:1.27-alpine", env=None, command=None):
    return NS(name=name, image=image, env=env or [], args=None, command=command)


def _template(*containers):
    return NS(spec=NS(containers=list(containers)), metadata=NS(labels={"app": "dummy-app"}))


def _rs(rev, *, deploy="dummy-app", ns="daalu-demo", created=None, image="nginx:1.27-alpine",
        env=None, command=None, ready=1, replicas=1):
    return NS(
        metadata=NS(
            name=f"{deploy}-{rev}", namespace=ns,
            annotations={"deployment.kubernetes.io/revision": str(rev)},
            owner_references=[NS(kind="Deployment", name=deploy, controller=True)],
            creation_timestamp=created or NOW,
            labels={"pod-template-hash": f"h{rev}"},
        ),
        spec=NS(replicas=replicas, template=_template(_container(image=image, env=env, command=command))),
        status=NS(ready_replicas=ready),
    )


def _event(reason, msg, *, kind="Deployment", name="dummy-app", ns="daalu-demo", when=None, count=1):
    return NS(
        involved_object=NS(kind=kind, name=name, namespace=ns),
        last_timestamp=when or NOW, event_time=None, metadata=NS(creation_timestamp=when or NOW),
        type="Normal", reason=reason, message=msg, count=count,
    )


class ApiError(Exception):
    def __init__(self, status, reason="err"):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.body = reason


class FakeCustom:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def list_cluster_custom_object(self, **kw):
        assert kw["group"] == "argoproj.io" and kw["plural"] == "applications"
        if self.error:
            raise self.error
        return self.result


class FakeClientMod:
    def __init__(self, custom):
        self._custom = custom

    def CustomObjectsApi(self, api_client=None):  # noqa: N802 — mirrors kubernetes.client
        return self._custom


class FakeCore:
    api_client = None

    def __init__(self, events=(), pods=(), pod=None):
        self.events, self.pods, self.pod = list(events), list(pods), pod
        self.calls: list[tuple[str, dict]] = []

    def list_namespaced_event(self, **kw):
        self.calls.append(("list_namespaced_event", kw))
        return NS(items=self.events)

    def list_event_for_all_namespaces(self, **kw):
        self.calls.append(("list_event_for_all_namespaces", kw))
        return NS(items=self.events)

    def list_namespaced_pod(self, **kw):
        return NS(items=self.pods)

    def read_namespaced_pod(self, **kw):
        return self.pod

    def read_namespaced_config_map(self, **kw):
        return {"metadata": {"name": kw["name"], "resourceVersion": "1"}, "data": {"MODE": "a"}}

    def patch_namespaced_config_map(self, **kw):
        assert kw["dry_run"] == "All"
        return {"metadata": {"name": kw["name"], "resourceVersion": "2"},
                "data": {"MODE": kw["body"]["data"]["MODE"]}}


class FakeApps:
    api_client = None

    def __init__(self, rs=(), deployment=None, patch_error=None):
        self.rs, self.deployment, self.patch_error = list(rs), deployment, patch_error
        self.patched: list[dict] = []

    def list_namespaced_replica_set(self, **kw):
        return NS(items=[r for r in self.rs if r.metadata.namespace == kw["namespace"]])

    def list_replica_set_for_all_namespaces(self, **kw):
        return NS(items=self.rs)

    def read_namespaced_deployment(self, **kw):
        return self.deployment

    def read_namespaced_replica_set(self, **kw):
        return next(r for r in self.rs if r.metadata.name == kw["name"])

    def patch_namespaced_deployment(self, **kw):
        self.patched.append(kw)
        if self.patch_error:
            raise self.patch_error
        d = self.deployment
        return {"spec": {"replicas": d.spec.replicas, "template": {"spec": {"containers": [
            {"name": "dummy", "image": kw["body"]["spec"]["template"]["spec"]["containers"][0]["image"]}]}}},
            "metadata": {"generation": 9}}


def _deployment(replicas=1, strategy="Recreate", image="nginx:this-tag-does-not-exist-9.9.9"):
    return NS(
        metadata=NS(name="dummy-app", namespace="daalu-demo",
                    annotations={"deployment.kubernetes.io/revision": "3"}),
        spec=NS(replicas=replicas, selector=NS(match_labels={"app": "dummy-app"}),
                strategy=NS(type=strategy), template=_template(_container(image=image))),
        status=NS(ready_replicas=0),
    )


def _install(monkeypatch, core, apps, custom=None):
    from daalu_automation.core import kube_tools

    seen: dict[str, Any] = {}

    async def fake_kube(tenant_id, cluster_name=None):
        seen["tenant_id"], seen["cluster"] = tenant_id, cluster_name
        return core, apps, FakeClientMod(custom or FakeCustom(error=ApiError(404)))

    monkeypatch.setattr(kube_tools, "_tenant_kube_or_default", fake_kube)
    return seen


# ── recent_changes ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_changes_shows_image_change_newest_first(monkeypatch):
    from daalu_automation.core.aiops import change_context

    rs = [
        _rs(1, created=NOW - timedelta(days=3)),
        _rs(2, created=NOW - timedelta(hours=5), command=["sh", "-c", "exit 1"], ready=0),
        _rs(3, created=NOW - timedelta(minutes=10), image="nginx:this-tag-does-not-exist-9.9.9", ready=0),
        _rs(1, deploy="other", ns="elsewhere", created=NOW - timedelta(minutes=1)),
    ]
    events = [
        _event("ScalingReplicaSet", "Scaled up replica set dummy-app-3 to 1", when=NOW - timedelta(minutes=9)),
        _event("ScalingReplicaSet", "old", when=NOW - timedelta(days=2)),
        _event("BackOff", "pod event", kind="Pod"),
    ]
    core = FakeCore(events=events)
    seen = _install(monkeypatch, core, FakeApps(rs=rs))
    out = await change_context.recent_changes(
        tenant_id="t1", cluster="dev-1", namespace="daalu-demo", hours=24)
    lines = out.splitlines()
    assert lines[0].startswith("Recent changes in namespace daalu-demo, last 24h")
    assert seen == {"tenant_id": "t1", "cluster": "dev-1"}
    # newest first: the scale event (9m ago), the revision 3 rollout (10m), revision 2 (5h)
    assert "ScalingReplicaSet" in lines[1] and "dummy-app-3" in lines[1]
    assert "revision 2 → 3" in lines[2] and "nginx:1.27-alpine → nginx:this-tag-does-not-exist-9.9.9" in lines[2]
    assert "[current]" in lines[2] and "0/1 ready" in lines[2]
    assert "revision 1 → 2" in lines[3] and "dummy command" in lines[3]
    assert "old" not in out and "pod event" not in out and "other" not in out
    assert "revision 1 created" not in out  # outside the window
    assert "Argo" not in out  # CRD absent is silent
    assert core.calls[0][1]["field_selector"] == "involvedObject.kind=Deployment"


@pytest.mark.asyncio
async def test_recent_changes_cluster_wide_and_argo(monkeypatch):
    from daalu_automation.core.aiops import change_context

    iso = (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    apps_ = {"items": [
        {"metadata": {"name": "shop"}, "spec": {"destination": {"namespace": "shop"}},
         "status": {"history": [
             {"id": 7, "revision": "abcdef1234567890", "deployedAt": iso,
              "source": {"repoURL": "https://git/shop.git", "targetRevision": "main"}},
             {"id": 6, "revision": "old", "deployedAt": "2020-01-01T00:00:00Z", "source": {}},
         ], "operationState": {"phase": "Failed", "finishedAt": iso, "message": "hook failed",
                               "syncResult": {"revision": "abcdef1234567890"}}}},
    ]}
    rs = [_rs(1, deploy="other", ns="elsewhere", created=NOW - timedelta(minutes=1))]
    _install(monkeypatch, FakeCore(), FakeApps(rs=rs), FakeCustom(result=apps_))
    out = await change_context.recent_changes(tenant_id=None, cluster=None, namespace=None, hours=2)
    assert "all namespaces" in out.splitlines()[0]
    assert "deployment/elsewhere/other rollout revision 1 created" in out
    assert "argocd app/shop synced revision abcdef123456 from https://git/shop.git @main" in out
    assert "last operation Failed" in out and "hook failed" in out
    assert "old" not in out.replace("deployment/elsewhere", "")

    # Namespace filter applies to the Argo destination.
    out = await change_context.recent_changes(tenant_id=None, cluster=None, namespace="daalu-demo")
    assert "argocd" not in out and "No Deployment rollouts" in out


@pytest.mark.asyncio
async def test_recent_changes_degrades_gracefully(monkeypatch):
    from daalu_automation.core import kube_tools
    from daalu_automation.core.aiops import change_context

    class BrokenApps(FakeApps):
        def list_namespaced_replica_set(self, **kw):
            raise ApiError(403, "Forbidden")

    _install(monkeypatch, FakeCore(), BrokenApps(), FakeCustom(error=ApiError(403)))
    out = await change_context.recent_changes(tenant_id=None, cluster=None, namespace="x")
    assert "note: ReplicaSets not readable" in out and "RBAC" in out

    async def no_kube(*a, **k):
        raise kube_tools.KubeUnavailable("no creds")

    monkeypatch.setattr(kube_tools, "_tenant_kube_or_default", no_kube)
    out = await change_context.recent_changes(tenant_id=None, cluster=None, namespace="x")
    assert out.startswith("error: kube client unavailable")


@pytest.mark.asyncio
async def test_recent_changes_is_a_registered_read_tool(monkeypatch):
    from daalu_automation.core import kube_tools
    from daalu_automation.core.aiops import change_context

    assert "recent_changes" in kube_tools.TOOLS
    assert not kube_tools.tool_requires_approval("recent_changes", {})
    names = {d["function"]["name"] for d in kube_tools.openai_tool_definitions()}
    assert "recent_changes" in names

    got: dict[str, Any] = {}

    async def fake(**kw):
        got.update(kw)
        return "ok"

    monkeypatch.setattr(change_context, "recent_changes", fake)
    out = await kube_tools.execute_tool(
        "recent_changes", {"namespace": "daalu-demo", "hours": 6},
        tenant_id="tid", cluster_name="dev-1",
    )
    assert out == "ok"
    assert got == {"tenant_id": "tid", "cluster": "dev-1", "namespace": "daalu-demo", "hours": 6}


# ── preview_write ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_rollout_undo(monkeypatch):
    from daalu_automation.core.aiops.preview import preview_write

    rs = [_rs(2, created=NOW - timedelta(days=1)),
          _rs(3, image="nginx:this-tag-does-not-exist-9.9.9")]
    _install(monkeypatch, FakeCore(), FakeApps(rs=rs, deployment=_deployment(replicas=2)))
    p = await preview_write("rollout_undo", {"namespace": "daalu-demo", "name": "dummy-app"},
                            tenant_id=None, cluster=None)
    assert set(p) == {"summary", "blast_radius", "details"}
    assert "revision 3 to 2" in p["summary"]
    assert "nginx:this-tag-does-not-exist-9.9.9 → nginx:1.27-alpine" in p["summary"]
    assert p["blast_radius"] == {"pods": 2, "deployments": 1}
    assert p["details"]["target_revision"] == 2


@pytest.mark.asyncio
async def test_preview_scale_restart_delete(monkeypatch):
    from daalu_automation.core.aiops.preview import preview_write

    rs = [_rs(3)]
    pod = NS(
        metadata=NS(name="dummy-app-3-abc", owner_references=[
            NS(kind="ReplicaSet", name="dummy-app-3", controller=True)]),
        status=NS(phase="Running"), spec=NS(node_name="n1"),
    )
    pods = [NS(metadata=NS(name=f"p{i}")) for i in range(3)]
    _install(monkeypatch, FakeCore(pods=pods, pod=pod), FakeApps(rs=rs, deployment=_deployment(replicas=3)))
    inp = {"namespace": "daalu-demo", "name": "dummy-app"}

    p = await preview_write("scale_deployment", {**inp, "replicas": 1}, tenant_id=None, cluster=None)
    assert "from 3 to 1" in p["summary"] and p["blast_radius"] == {"pods": 2, "deployments": 1}
    p = await preview_write("scale_deployment", {**inp, "replicas": 0}, tenant_id=None, cluster=None)
    assert "offline" in p["summary"]

    p = await preview_write("restart_deployment", inp, tenant_id=None, cluster=None)
    assert p["blast_radius"]["pods"] == 3 and "Recreate" in p["summary"]
    assert p["details"]["pods_recycled"] == ["p0", "p1", "p2"]

    p = await preview_write("delete_pod", {"namespace": "daalu-demo", "name": "dummy-app-3-abc"},
                            tenant_id=None, cluster=None)
    assert p["details"]["recreated"] is True and p["details"]["deployment"] == "dummy-app"
    assert "recreates it" in p["summary"]


@pytest.mark.asyncio
async def test_preview_patch_uses_server_dry_run(monkeypatch):
    from daalu_automation.core.aiops.preview import preview_write

    apps = FakeApps(deployment=_deployment(replicas=2))
    _install(monkeypatch, FakeCore(), apps)
    patch = {"spec": {"template": {"spec": {"containers": [{"name": "dummy", "image": "nginx:1.27-alpine"}]}}}}

    # The Deployment read returns a model object; give the fake a dict view.
    d = apps.deployment
    apps.read_namespaced_deployment = lambda **kw: {
        "spec": {"replicas": d.spec.replicas, "template": {"spec": {"containers": [
            {"name": "dummy", "image": "nginx:this-tag-does-not-exist-9.9.9"}]}}},
        "metadata": {"generation": 8}}
    p = await preview_write("patch_resource", {"namespace": "daalu-demo", "kind": "Deployment",
                                               "name": "dummy-app", "patch": patch},
                            tenant_id=None, cluster=None)
    assert apps.patched and apps.patched[0]["dry_run"] == "All"
    assert p["details"]["changed_keys"] == ["spec.template.spec.containers[0].image"]
    assert p["blast_radius"] == {"pods": 2, "deployments": 1}
    assert "Server-side dry run OK" in p["summary"]

    apps.patch_error = ApiError(422, "Unprocessable Entity")
    p = await preview_write("patch_resource", {"namespace": "daalu-demo", "kind": "Deployment",
                                               "name": "dummy-app", "patch": patch},
                            tenant_id=None, cluster=None)
    assert "REJECTED" in p["summary"] and p["details"]["dry_run"] == "rejected"

    # ConfigMap: changed data key, no pods recycled; noise keys ignored.
    p = await preview_write("patch_resource", {"namespace": "daalu-demo", "kind": "ConfigMap",
                                               "name": "cfg", "patch": {"data": {"MODE": "b"}}},
                            tenant_id=None, cluster=None)
    assert p["details"]["changed_keys"] == ["data.MODE"]
    assert p["blast_radius"] == {"pods": 0, "deployments": 0}


@pytest.mark.asyncio
async def test_preview_never_raises(monkeypatch):
    from daalu_automation.core import kube_tools
    from daalu_automation.core.aiops.preview import preview_write

    async def boom(*a, **k):
        raise RuntimeError("cluster unreachable")

    monkeypatch.setattr(kube_tools, "_tenant_kube_or_default", boom)
    assert await preview_write("rollout_undo", {"namespace": "a", "name": "b"},
                               tenant_id=None, cluster=None) == {}
    assert await preview_write("call_external_api", {"method": "POST"},
                               tenant_id=None, cluster=None) == {}
    _install(monkeypatch, FakeCore(), FakeApps(deployment=None))
    assert await preview_write("scale_deployment", {"namespace": "a", "name": "b", "replicas": 1},
                               tenant_id=None, cluster=None) == {}
