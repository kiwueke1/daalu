"""Unit tests for the NV-CM (network) + Tinkerbell (server) integration.

Pure-unit (no Postgres / no live NV-CM / no kube): exercises routing, the
Keycloak token cache, the NV-CM executor's deterministic render + diff, the
Tinkerbell provisioning spec builder + executor success path (fake client),
the NATS bridge mapping, and the controller's values + helm argv. The
DB-backed gate paths are covered in test_change_proposals.py.
"""

from __future__ import annotations

import uuid

from daalu_automation.core.sot.models import (
    Device,
    InterfaceConfig,
    NetworkFacts,
    VlanDefinition,
)

# ── routing ──────────────────────────────────────────────────────────────


def test_routing_switch_goes_to_nvcm_when_enabled():
    from daalu_automation.core.device import routing

    leaf = Device(id="d1", name="leaf01", transport="junos")
    assert (
        routing.select_config_transport(leaf, config_manager_enabled=True)
        == "config_manager"
    )
    assert (
        routing.select_config_transport(leaf, config_manager_enabled=False) == "junos"
    )


def test_routing_server_provisioning_goes_to_tinkerbell():
    from daalu_automation.core.device import routing

    host = Device(id="s1", name="host01", transport="linux_ssh")
    assert (
        routing.select_provision_transport(host, server_management_enabled=True)
        == "tinkerbell"
    )
    assert (
        routing.select_provision_transport(host, server_management_enabled=False)
        is None
    )


def test_routing_default_unchanged_without_flags():
    from daalu_automation.core.device import routing

    host = Device(id="s1", name="host01", transport="linux_ssh")
    assert routing.select_config_transport(host, config_manager_enabled=True) == "linux_ssh"


# ── keycloak token cache ──────────────────────────────────────────────────


async def test_keycloak_token_cached(monkeypatch):
    from daalu_automation.core import keycloak

    keycloak.clear_token_cache()
    calls = {"n": 0}

    class _Resp:
        status_code = 200

        def json(self):
            calls["n"] += 1
            return {"access_token": f"tok{calls['n']}", "expires_in": 300}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(keycloak.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        keycloak, "_resolve_token_url", lambda: "https://kc/token"
    )
    t1 = await keycloak.get_machine_token(client_id="hub", client_secret="s", audience="nv")
    t2 = await keycloak.get_machine_token(client_id="hub", client_secret="s", audience="nv")
    assert t1 == t2 == "tok1"
    assert calls["n"] == 1  # second call served from cache
    keycloak.clear_token_cache()


# ── NV-CM executor render/diff ────────────────────────────────────────────


async def test_nvcm_executor_render_deterministic_and_sorted():
    from daalu_automation.core.device import get_device_adapter

    ex = get_device_adapter("config_manager")
    facts = NetworkFacts(
        hostname="leaf01",
        interfaces=[InterfaceConfig(name="Ethernet2"), InterfaceConfig(name="Ethernet1")],
        vlans=[VlanDefinition(vlan_id=20), VlanDefinition(vlan_id=10, name="prod")],
    )
    r1 = await ex.render(facts)
    r2 = await ex.render(facts)
    assert r1.files == r2.files
    txt = r1.files["startup.yaml"]
    assert txt.index("Ethernet1") < txt.index("Ethernet2")
    assert txt.index("vlan 10") < txt.index("vlan 20")
    d = await ex.diff(NetworkFacts(), facts)
    assert d.has_changes


# ── Tinkerbell provisioning spec + executor ───────────────────────────────


def test_build_provision_spec_from_device():
    from daalu_automation.core.sot.server_provisioning import build_provision_spec

    dev = Device(
        id="dev-uuid", name="GPU Node 01", primary_ip="10.10.0.20",
        transport="linux_ssh",
        extra={"mac": "aa:bb:cc:dd:ee:01", "bmc_ip": "192.168.0.50",
               "bmc_user": "root", "bmc_password": "calvin", "disk": "/dev/nvme0n1"},
    )
    spec, secret = build_provision_spec(
        dev, namespace="tink-system",
        os_image_url="http://10.10.0.9/ubuntu.raw.gz",
        cloud_init_userdata="#cloud-config\nhostname: gpu-node-01\n",
    )
    assert spec.hardware_name == "gpu-node-01"
    assert spec.hardware["spec"]["interfaces"][0]["dhcp"]["mac"] == "aa:bb:cc:dd:ee:01"
    assert spec.hardware["spec"]["disks"][0]["device"] == "/dev/nvme0n1"
    assert secret["stringData"]["password"] == "calvin"
    assert spec.power_job_tasks[1]["oneTimeBootDeviceAction"]["device"] == ["pxe"]


async def test_tinkerbell_provision_success_path(monkeypatch):
    import daalu_automation.core.device.tinkerbell_provision as tp

    class FakeTk:
        def __init__(self, **k):
            self.applied = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def apply(self, kind, body):
            self.applied.append(kind)

        def build_power_job(self, *, name, machine_ref, tasks, namespace=None):
            return {"kind": "Job", "metadata": {"name": name}}

        async def rufio_job_state(self, name):
            return "Completed"

        async def workflow_state(self, name):
            return "STATE_SUCCESS"

    monkeypatch.setattr(tp, "TinkerbellClient", FakeTk)
    monkeypatch.setattr(tp, "_POLL_INTERVAL_S", 0.001)
    spec = tp.ServerProvisionSpec(
        hardware_name="node01", workflow_name="node01-provision",
        rufio_machine_name="node01-bmc",
        hardware={"kind": "Hardware", "metadata": {"name": "node01"}},
        workflow={"kind": "Workflow", "metadata": {"name": "node01-provision"}},
        power_job_tasks=[{"powerAction": "on"}],
    )
    res = await tp.provision(spec, kubeconfig=None, namespace="tink-system")
    assert res.success, res.error
    steps = [s.get("step") for s in res.per_step]
    assert "apply_hardware" in steps and "workflow" in steps


async def test_tinkerbell_provision_fails_on_workflow_failure(monkeypatch):
    import daalu_automation.core.device.tinkerbell_provision as tp

    class FakeTk:
        def __init__(self, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def apply(self, kind, body):
            pass

        def build_power_job(self, **k):
            return {"kind": "Job", "metadata": {"name": "j"}}

        async def rufio_job_state(self, name):
            return "Completed"

        async def workflow_state(self, name):
            return "STATE_FAILED"

    monkeypatch.setattr(tp, "TinkerbellClient", FakeTk)
    monkeypatch.setattr(tp, "_POLL_INTERVAL_S", 0.001)
    spec = tp.ServerProvisionSpec(
        hardware_name="n", workflow_name="n-wf",
        workflow={"kind": "Workflow", "metadata": {"name": "n-wf"}},
    )
    res = await tp.provision(spec, kubeconfig=None, namespace="tink-system")
    assert not res.success
    assert "STATE_FAILED" in res.error


# ── host_cluster_ready precheck (Tier-A singletons) ────────────────────────


def test_host_precheck_ready_when_all_present():
    from daalu_automation.config_manager_controller.prechecks import (
        REQUIRED_CRDS,
        evaluate_readiness,
    )

    ready, missing = evaluate_readiness(
        present_crds=set(REQUIRED_CRDS), gatewayclass_present=True
    )
    assert ready is True
    assert missing == []


def test_host_precheck_reports_missing_crd_labels():
    from daalu_automation.config_manager_controller.prechecks import (
        evaluate_readiness,
    )

    # Only CNPG installed → cert-manager + Gateway API labels come back.
    ready, missing = evaluate_readiness(
        present_crds={"clusters.postgresql.cnpg.io"},
        gatewayclass_present=False,
    )
    assert ready is False
    assert any("Envoy Gateway" in m for m in missing)
    assert any("cert-manager" in m for m in missing)
    # CNPG is present, so its label must NOT appear.
    assert not any("CloudNativePG" in m for m in missing)


def test_host_precheck_flags_missing_gatewayclass_object():
    from daalu_automation.config_manager_controller.prechecks import (
        REQUIRED_CRDS,
        evaluate_readiness,
    )

    # All CRDs present but the shared GatewayClass object is absent: the
    # CRD being installed isn't enough — the chart references it by name.
    ready, missing = evaluate_readiness(
        present_crds=set(REQUIRED_CRDS), gatewayclass_present=False
    )
    assert ready is False
    assert any("GatewayClass" in m for m in missing)


# ── Tinkerbell reachability probe + health precheck ────────────────────────


async def test_tinkerbell_probe_lists_workflows(monkeypatch):
    from daalu_automation.core.tinkerbell import client as tk_client

    seen = {}

    class FakeCO:
        async def list_namespaced_custom_object(self, **kw):
            seen.update(kw)
            return {"items": []}

    tk = tk_client.TinkerbellClient(namespace="tink-system")
    monkeypatch.setattr(tk, "_co", lambda: FakeCO())
    await tk.probe()
    assert seen["plural"] == "workflows"
    assert seen["namespace"] == "tink-system"
    assert seen["limit"] == 1


async def test_tinkerbell_probe_raises_on_api_error(monkeypatch):
    from daalu_automation.core.tinkerbell import client as tk_client

    class ApiErr(Exception):
        status = 403
        reason = "Forbidden"

    monkeypatch.setattr(
        tk_client.k8s_client.exceptions, "ApiException", ApiErr, raising=False
    )

    class FakeCO:
        async def list_namespaced_custom_object(self, **kw):
            raise ApiErr()

    tk = tk_client.TinkerbellClient(namespace="tink-system")
    monkeypatch.setattr(tk, "_co", lambda: FakeCO())
    try:
        await tk.probe()
        raised = False
    except tk_client.TinkerbellError as e:
        raised = True
        assert "unreachable" in str(e)
    assert raised


async def test_tinkerbell_health_false_when_no_integration():
    from daalu_automation.core.tinkerbell.health import check_health

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _FakeDB:
        async def execute(self, *a, **k):
            return _Result()

    ok, msg = await check_health(_FakeDB(), uuid.uuid4())
    assert ok is False
    assert "no tinkerbell integration" in msg


# ── NATS bridge mapping ────────────────────────────────────────────────────


def test_nats_bridge_maps_subjects():
    from daalu_automation.workers.nats_bridge import _map_subject_to_event

    e = _map_subject_to_event(
        "t1", "nv-config-manager.workflow.result", {"status": "FAILED"}
    )
    assert e and e.type == "config_manager.workflow.result" and e.severity == "warning"
    e2 = _map_subject_to_event("t1", "nv-config-manager.nautobotchange", {})
    assert e2 and e2.type == "config_manager.device_change" and e2.severity == "info"
    assert _map_subject_to_event("t1", "unrelated.subject", {}) is None


# ── controller values + helm argv ──────────────────────────────────────────


def test_render_values_multitenant_safe():
    from daalu_automation.config_manager_controller import values as cm_values
    from daalu_automation.models import ConfigManagerTenant

    row = ConfigManagerTenant(
        tenant_id=uuid.uuid4(), namespace="cm-acme",
        base_hostname="acme.cm.daalu.io",
        components={"render": True, "ztp": False}, size_profile="small",
        chart_version="1.3.0",
    )
    vals = cm_values.render_values(row, harbor_registry="harbor.daalu.io")
    assert vals["gateway"]["createGatewayClass"] is False
    assert vals["gateway"]["baseHostname"] == "acme.cm.daalu.io"
    assert vals["global"]["namespace"] == "cm-acme"
    assert vals["cnpg"]["instances"] == 1
    # No Vault in daalu → native-kubernetes secrets method (avoids the
    # chart's required secrets.vault.paths.* under the default 'eso' method).
    assert vals["secrets"]["method"] == "kubernetes"
    # Bundled (in-namespace) Nautobot/NATS/Redis — avoids the chart's
    # externalServices.<svc>.server/host requirement (local=false default).
    assert vals["externalServices"]["nautobot"]["local"] is True
    assert vals["externalServices"]["redis"]["local"] is True
    assert vals["externalServices"]["nats"]["local"] is True
    # Images repointed at the Harbor mirror (the chart has no
    # global.imageRegistry knob, so per-image repository overrides are the
    # only thing that works). Tags are left to the chart default.
    imgs = vals["global"]["images"]
    assert (
        imgs["nvConfigManager"]["repository"]
        == "harbor.daalu.io/nv-config-manager/nv-config-manager"
    )
    assert imgs["redis"]["repository"] == "harbor.daalu.io/nv-config-manager/redis"
    assert "tag" not in imgs["redis"]  # inherited from chart default
    # No mirror configured → no image overrides (dev/local path).
    bare = cm_values.render_values(row)
    assert "images" not in bare.get("global", {})
    urls = cm_values.compute_urls("acme.cm.daalu.io")
    # Machine svc-* URLs are plain HTTP — the tenant gateway is HTTP-only
    # (hub terminates TLS + reverse-proxies over the tunnel).
    assert urls["config_store_url"] == "http://svc-config-store.acme.cm.daalu.io"
    # Browser-facing human URLs stay HTTPS (terminated at the hub wildcard).
    assert urls["nautobot_human"] == "https://nautobot-acme.cm.daalu.io"


async def test_helm_runner_argv(monkeypatch):
    from daalu_automation.config_manager_controller.helm_runner import HelmRunner

    captured = {}

    async def fake_runner(argv, env):
        captured["argv"] = argv
        captured["kube"] = env.get("KUBECONFIG")
        return 0, "deployed", ""

    hr = HelmRunner(chart_path="deploy/charts/nv-config-manager-1.3.0", runner=fake_runner)
    res = await hr.upgrade_install(
        release="cm-x", namespace="cm-acme", values={"a": 1},
        kubeconfig={"apiVersion": "v1"}, wait=True,
    )
    assert res.ok
    a = captured["argv"]
    assert a[:3] == ["helm", "upgrade", "--install"]
    assert "cm-x" in a and "cm-acme" in a and "--wait" in a
    assert captured["kube"]  # KUBECONFIG set for remote


def test_provision_op_enum_and_service_purpose():
    from daalu_automation.models import ChangeProposalKind

    assert ChangeProposalKind.provision_op.value == "provision_op"
    # the controller token purpose is accepted by the verifier's allowlist
    import inspect

    from daalu_automation.core import service_tokens

    src = inspect.getsource(service_tokens.verify_service_token)
    assert "config-manager-provision" in src
