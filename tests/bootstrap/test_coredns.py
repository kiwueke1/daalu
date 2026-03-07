# tests/bootstrap/test_coredns.py
# Tests for KubeCoreDNSPatchComponent and _strip_hosts_block

from __future__ import annotations

import pytest
from daalu.bootstrap.infrastructure.components.kube_coredns import (
    KubeCoreDNSPatchComponent,
    _strip_hosts_block,
)


# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------

MINIMAL_COREFILE = """\
.:53 {
    errors
    health {
       lameduck 5s
    }
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
       pods insecure
       fallthrough in-addr.arpa ip6.arpa
       ttl 30
    }
    prometheus :9153
    forward . /etc/resolv.conf
    cache 30
    loop
    reload
    loadbalance
}
"""


class FakeKubectl:
    """Minimal kubectl fake for CoreDNS component tests."""

    def __init__(
        self,
        *,
        gateway_ip: str = "10.10.1.101",
        vs_output: str = "",
        corefile: str = MINIMAL_COREFILE,
        rollout_rc: int = 0,
    ):
        self._gateway_ip = gateway_ip
        self._vs_output = vs_output
        self._corefile = corefile
        self._rollout_rc = rollout_rc
        self.patches: list[dict] = []
        self._run_calls: list[str] = []

    def _run(self, cmd: str):
        self._run_calls.append(cmd)
        if "get svc istio-ingressgateway" in cmd:
            return (0, self._gateway_ip, "")
        if "get virtualservices" in cmd:
            return (0, self._vs_output, "")
        if "get cm coredns" in cmd:
            return (0, self._corefile, "")
        if "rollout restart" in cmd:
            return (0, "", "")
        if "rollout status" in cmd:
            return (self._rollout_rc, "", "rollout failed" if self._rollout_rc != 0 else "")
        return (0, "", "")

    def patch(self, *, api_version, kind, name, namespace, patch):
        self.patches.append({"name": name, "namespace": namespace, "patch": patch})


# ---------------------------------------------------------------------------
# _strip_hosts_block
# ---------------------------------------------------------------------------

def test_strip_hosts_block_noop_when_no_block():
    original = ".:53 {\n    prometheus :9153\n    forward . /etc/resolv.conf\n}\n"
    assert _strip_hosts_block(original) == original


def test_strip_hosts_block_removes_simple_hosts_block():
    corefile = (
        ".:53 {\n"
        "    hosts {\n"
        "       10.10.1.101 auth.daalu.io\n"
        "       fallthrough\n"
        "    }\n"
        "    prometheus :9153\n"
        "}\n"
    )
    result = _strip_hosts_block(corefile)
    assert "hosts" not in result
    assert "auth.daalu.io" not in result
    assert "prometheus :9153" in result


def test_strip_hosts_block_removes_template_block():
    corefile = (
        ".:53 {\n"
        "    template IN A daalu.io {\n"
        "       match .*\n"
        "       answer \"{{ .Name }} 60 IN A 10.10.1.101\"\n"
        "    }\n"
        "    forward . /etc/resolv.conf\n"
        "}\n"
    )
    result = _strip_hosts_block(corefile)
    assert "template" not in result
    assert "forward . /etc/resolv.conf" in result


def test_strip_hosts_block_handles_nested_braces():
    corefile = (
        ".:53 {\n"
        "    hosts {\n"
        "       10.0.0.1 a.io\n"
        "       # comment { } with braces\n"
        "       fallthrough\n"
        "    }\n"
        "    prometheus :9153\n"
        "}\n"
    )
    result = _strip_hosts_block(corefile)
    assert "hosts" not in result
    assert "prometheus :9153" in result


def test_strip_hosts_block_idempotent_on_no_block():
    result1 = _strip_hosts_block(MINIMAL_COREFILE)
    result2 = _strip_hosts_block(result1)
    assert result1 == result2


def test_strip_hosts_block_removes_and_cleans_twice():
    """Apply twice after inserting hosts block — should be idempotent."""
    with_block = (
        ".:53 {\n"
        "    hosts {\n"
        "       10.10.1.101 identity.daalu.io\n"
        "       fallthrough\n"
        "    }\n"
        "    prometheus :9153\n"
        "}\n"
    )
    cleaned = _strip_hosts_block(with_block)
    cleaned_again = _strip_hosts_block(cleaned)
    assert cleaned == cleaned_again
    assert "identity.daalu.io" not in cleaned


# ---------------------------------------------------------------------------
# _build_corefile
# ---------------------------------------------------------------------------

def test_build_corefile_inserts_hosts_before_prometheus():
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    result = comp._build_corefile(
        MINIMAL_COREFILE,
        gateway_ip="10.10.1.101",
        hostnames=["auth.daalu.io", "identity.daalu.io"],
    )
    assert "hosts {" in result
    assert "10.10.1.101 auth.daalu.io" in result
    assert "10.10.1.101 identity.daalu.io" in result
    assert "fallthrough" in result
    # hosts block appears before prometheus
    hosts_pos = result.index("hosts {")
    prometheus_pos = result.index("prometheus")
    assert hosts_pos < prometheus_pos


def test_build_corefile_inserts_before_forward_when_no_prometheus():
    corefile = ".:53 {\n    forward . /etc/resolv.conf\n}\n"
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    result = comp._build_corefile(
        corefile,
        gateway_ip="10.0.0.1",
        hostnames=["svc.example.com"],
    )
    assert "hosts {" in result
    hosts_pos = result.index("hosts {")
    forward_pos = result.index("forward .")
    assert hosts_pos < forward_pos


def test_build_corefile_is_idempotent():
    """Running _build_corefile twice produces the same result."""
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    first = comp._build_corefile(
        MINIMAL_COREFILE,
        gateway_ip="10.10.1.101",
        hostnames=["auth.daalu.io"],
    )
    second = comp._build_corefile(
        first,
        gateway_ip="10.10.1.101",
        hostnames=["auth.daalu.io"],
    )
    assert first == second


def test_build_corefile_updates_ip_on_second_run():
    """Re-running with a new IP replaces old IP in the hosts block."""
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    first = comp._build_corefile(
        MINIMAL_COREFILE,
        gateway_ip="10.10.1.101",
        hostnames=["auth.daalu.io"],
    )
    second = comp._build_corefile(
        first,
        gateway_ip="10.10.2.200",
        hostnames=["auth.daalu.io"],
    )
    assert "10.10.2.200 auth.daalu.io" in second
    assert "10.10.1.101" not in second


# ---------------------------------------------------------------------------
# _collect_vs_hostnames
# ---------------------------------------------------------------------------

def test_collect_vs_hostnames_filters_cluster_local():
    vs_out = "identity.daalu.io keystone-api.openstack.svc.cluster.local\n"
    kubectl = FakeKubectl(vs_output=vs_out)
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    hosts = comp._collect_vs_hostnames(kubectl)
    assert "identity.daalu.io" in hosts
    assert "keystone-api.openstack.svc.cluster.local" not in hosts


def test_collect_vs_hostnames_filters_wildcards():
    kubectl = FakeKubectl(vs_output="*.daalu.io auth.daalu.io\n")
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    hosts = comp._collect_vs_hostnames(kubectl)
    assert "*.daalu.io" not in hosts
    assert "auth.daalu.io" in hosts


def test_collect_vs_hostnames_filters_single_label():
    kubectl = FakeKubectl(vs_output="keystone-api auth.daalu.io\n")
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    hosts = comp._collect_vs_hostnames(kubectl)
    assert "keystone-api" not in hosts
    assert "auth.daalu.io" in hosts


def test_collect_vs_hostnames_sorted():
    kubectl = FakeKubectl(vs_output="z.daalu.io a.daalu.io m.daalu.io\n")
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    hosts = comp._collect_vs_hostnames(kubectl)
    assert hosts == sorted(hosts)


def test_collect_vs_hostnames_returns_empty_on_kubectl_error():
    class ErrorKubectl:
        def _run(self, cmd):
            return (1, "", "error")
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    hosts = comp._collect_vs_hostnames(ErrorKubectl())
    assert hosts == []


# ---------------------------------------------------------------------------
# _get_gateway_ip
# ---------------------------------------------------------------------------

def test_get_gateway_ip_raises_when_empty():
    class NoIpKubectl:
        def _run(self, cmd):
            return (0, "", "")
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    with pytest.raises(RuntimeError, match="Could not determine Istio gateway IP"):
        comp._get_gateway_ip(NoIpKubectl())


def test_get_gateway_ip_raises_on_nonzero_rc():
    class FailKubectl:
        def _run(self, cmd):
            return (1, "", "svc not found")
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    with pytest.raises(RuntimeError):
        comp._get_gateway_ip(FailKubectl())


def test_get_gateway_ip_returns_stripped_ip():
    kubectl = FakeKubectl(gateway_ip="10.10.1.101")
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    ip = comp._get_gateway_ip(kubectl)
    assert ip == "10.10.1.101"


# ---------------------------------------------------------------------------
# post_install integration
# ---------------------------------------------------------------------------

def test_post_install_patches_coredns_and_restarts():
    kubectl = FakeKubectl(
        gateway_ip="10.10.1.101",
        vs_output="auth.daalu.io identity.daalu.io\n",
    )
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    comp.post_install(kubectl)

    # A patch was applied to the ConfigMap
    assert len(kubectl.patches) == 1
    patch = kubectl.patches[0]
    assert patch["name"] == "coredns"
    assert patch["namespace"] == "kube-system"
    corefile = patch["patch"]["data"]["Corefile"]
    assert "10.10.1.101 auth.daalu.io" in corefile
    assert "10.10.1.101 identity.daalu.io" in corefile

    # rollout restart and status were invoked
    cmds = " ".join(kubectl._run_calls)
    assert "rollout restart" in cmds
    assert "rollout status" in cmds


def test_post_install_skips_patch_when_no_virtualservices():
    kubectl = FakeKubectl(vs_output="")
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    comp.post_install(kubectl)
    # No patch should have been applied
    assert len(kubectl.patches) == 0


def test_post_install_raises_when_rollout_fails():
    kubectl = FakeKubectl(
        vs_output="auth.daalu.io\n",
        rollout_rc=1,
    )
    comp = KubeCoreDNSPatchComponent(kubeconfig="/tmp/fake.yaml")
    with pytest.raises(RuntimeError, match="CoreDNS rollout did not complete"):
        comp.post_install(kubectl)
