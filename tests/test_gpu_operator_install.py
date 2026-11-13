"""AI Factory → Add GPU installs the NVIDIA GPU Operator when a cluster has none.

These pin the settings that were found by hand on a real k3s GPU node and
that each fail quietly when wrong: the toolkit on k3s (node NotReady), the
DCGM ServiceMonitor and tenant label (AI Factory empty), and the "no NVIDIA
runtime" hint (a stuck pod with nothing to say why). Pure functions and a
fake helm; no cluster needed.
"""

from __future__ import annotations

import pytest
import yaml

from daalu_automation.gpu_controller import operator as op

TENANT = "00000000-0000-0000-0000-000000000010"

# ── distribution detection ─────────────────────────────────────────────


def test_detects_k3s_from_kubelet_or_runtime():
    assert op.is_k3s([{"kubelet_version": "v1.35.10+k3s1", "container_runtime_version": ""}])
    assert op.is_k3s(
        [{"kubelet_version": "v1.35.10", "container_runtime_version": "containerd://2.2.10-k3s1"}]
    )


def test_plain_kubeadm_is_not_k3s():
    assert not op.is_k3s(
        [{"kubelet_version": "v1.33.2", "container_runtime_version": "containerd://2.2.4"}]
    )
    assert not op.is_k3s([])


# ── helm values ────────────────────────────────────────────────────────


def test_k3s_keeps_the_operator_toolkit_off():
    """On k3s the operator's toolkit rewrites containerd's CNI paths → NotReady."""
    v = op.build_values(tenant_id=TENANT, k3s=True, servicemonitor=True)
    assert v["toolkit"] == {"enabled": False}
    assert v["driver"] == {"enabled": False}
    assert v["cdi"] == {"enabled": True}


def test_other_clusters_let_the_operator_manage_the_toolkit():
    v = op.build_values(tenant_id=TENANT, k3s=False, servicemonitor=True)
    assert "toolkit" not in v  # chart default: enabled
    assert v["cdi"] == {"enabled": True}


def test_dcgm_metrics_are_scraped_and_carry_the_tenant():
    """AI Factory's queries are tenant-scoped; unlabelled series show nothing."""
    sm = op.build_values(tenant_id=TENANT, k3s=True, servicemonitor=True)["dcgmExporter"][
        "serviceMonitor"
    ]
    assert sm["enabled"] is True
    assert sm["relabelings"] == [
        {"action": "replace", "targetLabel": "tenant", "replacement": TENANT}
    ]


def test_no_servicemonitor_without_the_prometheus_operator():
    """Asking for a ServiceMonitor with no CRD fails the whole helm install."""
    assert "dcgmExporter" not in op.build_values(tenant_id=TENANT, k3s=True, servicemonitor=False)


# ── status summary ─────────────────────────────────────────────────────


def _summary(**kw):
    base = dict(installed=True, gpu_nodes=0, k3s=True, pods_total=8, pods_ready=3, problem_messages=[])
    base.update(kw)
    return op.summarise(**base)


def test_phase_ready_once_a_gpu_is_advertised():
    assert _summary(gpu_nodes=1)["phase"] == "ready"


def test_phase_absent_before_install_and_installing_after():
    assert _summary(installed=False)["phase"] == "absent"
    assert _summary()["phase"] == "installing"


def test_missing_nvidia_runtime_on_k3s_gives_the_host_commands():
    s = _summary(
        problem_messages=[
            'Failed to create pod sandbox: rpc error: code = Unknown desc = '
            'failed to get sandbox runtime: no runtime for "nvidia" is configured'
        ]
    )
    assert s["phase"] == "blocked"
    assert "nvidia-container-toolkit" in s["host_commands"]
    assert "systemctl restart k3s" in s["host_commands"]


def test_missing_runtime_elsewhere_explains_without_k3s_commands():
    s = _summary(k3s=False, problem_messages=["unknown runtime handler nvidia"])
    assert s["phase"] == "blocked"
    assert s["host_commands"] is None
    assert "nvidia-ctk runtime configure" in s["hint"]


# ── helm invocation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_runs_helm_against_the_tunnel_kubeconfig():
    seen: dict = {}

    async def fake_helm(argv, env):
        seen["argv"] = argv
        values_path = argv[argv.index("-f") + 1]
        with open(values_path) as f:
            seen["values"] = yaml.safe_load(f)
        with open(env["KUBECONFIG"]) as f:
            seen["kubeconfig"] = yaml.safe_load(f)
        return 0, "deployed", ""

    kc = {"apiVersion": "v1", "clusters": [{"name": "c", "cluster": {"server": "https://10.200.0.2:6443"}}]}
    ok, err = await op.install(
        kubeconfig=kc, tenant_id=TENANT, k3s=True, servicemonitor=True, runner=fake_helm
    )
    assert ok and err == ""
    argv = seen["argv"]
    assert argv[:5] == ["helm", "upgrade", "--install", "gpu-operator", "gpu-operator"]
    assert argv[argv.index("--repo") + 1] == op.CHART_REPO
    assert argv[argv.index("--version") + 1] == "v25.10.1"
    assert argv[argv.index("-n") + 1] == "gpu-operator"
    assert "--wait" not in argv  # the form polls; helm must not hold the request
    assert seen["values"]["toolkit"] == {"enabled": False}
    assert seen["kubeconfig"]["clusters"][0]["cluster"]["server"] == "https://10.200.0.2:6443"


@pytest.mark.asyncio
async def test_install_reports_helms_error():
    async def failing_helm(argv, env):
        return 1, "", "Error: INSTALLATION FAILED: cannot reach repo"

    ok, err = await op.install(
        kubeconfig={}, tenant_id=TENANT, k3s=False, servicemonitor=False, runner=failing_helm
    )
    assert not ok
    assert "cannot reach repo" in err
