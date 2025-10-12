# daalu/src/daalu/bootstrap/setup_manager.py

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

from daalu.helm.cli_runner import HelmCliRunner
from daalu.config.models import ValuesRef, ReleaseSpec, RepoSpec
from .hosts_inventory import (
    build_hosts_entries,
    update_hosts_file,
    render_inventory_templates,
)

from ..observers.dispatcher import EventBus
from ..observers.events import (
    new_ctx,
    SetupStarted,
    KubeconfigGenerated,
    ControlPlaneDiscovered,
    CiliumInstalled,
    CiliumReady,
    HostsUpdated,
    NodesLabeled,
    SetupFailed,
    SetupSummary,
)
from dataclasses import dataclass


CILIUM_REPO = RepoSpec(name="cilium", url="https://helm.cilium.io/")


@dataclass(frozen=True)
class ControlPlaneDiscovered:
    cluster_name: str
    ip: str

@dataclass
class SetupOptions:
    cluster_name: str = "openstack-infra"
    workload_kubeconfig: Path = Path("/var/lib/tmp/kubeconfig")
    admin_conf: Path = Path("/etc/kubernetes/admin.conf")
    expected_cilium_pods: int = 5
    domain_suffix: str = "net.daalu.io"
    hosts_file: Path = Path("/etc/hosts")
    templates_dir: Path = Path("templates/setup/")
    output_inventory_dir: Path = Path("cloud-config/inventory")


class SetupManager:
    """
    Replaces the Ansible 'setup' playbooks with Python.
    - Generates kubeconfig for the workload cluster
    - Installs Cilium via Helm with computed k8sServiceHost/Port
    - Waits for Cilium pods to be Ready
    - Updates /etc/hosts and renders inventory templates
    - Labels/Taints nodes for bootstrap (optional remove later)
    """

    def __init__(self, repo_root: Path, mgmt_context: Optional[str] = None):
        self.repo_root = repo_root
        self.mgmt_context = mgmt_context

    # ---------- kubeconfig / control-plane IP ----------

    def generate_kubeconfig(self, opts: SetupOptions) -> None:
        opts.workload_kubeconfig.parent.mkdir(parents=True, exist_ok=True)
        # write kubeconfig for workload
        with opts.workload_kubeconfig.open("w") as f:
            cp = subprocess.run(
                ["clusterctl", "get", "kubeconfig", opts.cluster_name],
                capture_output=True, text=True, check=True
            )
            f.write(cp.stdout)

        # Also try to update /etc/kubernetes/admin.conf if writable
        try:
            opts.admin_conf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(opts.workload_kubeconfig, opts.admin_conf)
        except PermissionError:
            # non-root – OK to skip
            pass

    def get_control_plane_ip(self, opts: SetupOptions) -> str:
        cmd = [
            "kubectl", "--kubeconfig", str(opts.admin_conf),
            "get", "nodes", "-l", "node-role.kubernetes.io/control-plane",
            "-o", "json",
        ]
        cp = subprocess.run(cmd, capture_output=True, text=True, check=True)
        import json
        data = json.loads(cp.stdout or "{}")
        items = data.get("items", [])
        if not items:
            raise RuntimeError("No control-plane nodes found")
        addrs = items[0].get("status", {}).get("addresses", [])
        for a in addrs:
            if a.get("type") == "InternalIP":
                return a.get("address")
        raise RuntimeError("No InternalIP found for control-plane node")

    # ---------- Cilium via Helm ----------

    def install_cilium(self, opts: SetupOptions, control_plane_ip: str) -> None:
        # Helm should target the WORKLOAD cluster via KUBECONFIG=workload
        helm = HelmCliRunner(kube_context=None, debug=False)
        # Inject KUBECONFIG into environment just for these calls
        os.environ["KUBECONFIG"] = str(opts.workload_kubeconfig)

        # Add repo + install
        helm.add_repo(CILIUM_REPO)
        helm.update_repos()

        values = {
            "ipam": {"mode": "kubernetes"},
            "kubeProxyReplacement": True,
            "k8sServiceHost": control_plane_ip,
            "k8sServicePort": 6443,
            "hostServices": {"enabled": True},
            "externalIPs": {"enabled": True},
            "nodePort": {"enabled": True},
            "hostPort": {"enabled": True},
            "image": {"pullPolicy": "IfNotPresent"},
            "operator": {"replicas": 1},
            "prometheus": {"enabled": True},
            "hubble": {"enabled": True, "relay": {"enabled": True}, "ui": {"enabled": True}},
        }
        rel = ReleaseSpec(
            name="cilium",
            namespace="kube-system",
            chart="cilium/cilium",
            values=ValuesRef(inline=values),
            create_namespace=True,
            atomic=True,
            wait=True,
            timeout_seconds=900,
        )
        helm.lint(rel)
        helm.upgrade_install(rel)

    def wait_for_cilium(self, opts: SetupOptions, retries: int = 30, delay: int = 10) -> None:
        import json
        for _ in range(retries):
            cp = subprocess.run(
                ["kubectl", "--kubeconfig", str(opts.workload_kubeconfig),
                 "get", "pods", "-n", "kube-system", "-l", "k8s-app=cilium", "-o", "json"],
                capture_output=True, text=True, check=True,
            )
            data = json.loads(cp.stdout or "{}")
            items = data.get("items", [])
            ready = 0
            for pod in items:
                if pod.get("status", {}).get("phase") != "Running":
                    continue
                cs = pod.get("status", {}).get("containerStatuses", []) or []
                if cs and all(c.get("ready") for c in cs):
                    ready += 1
            if ready >= opts.expected_cilium_pods:
                return
            time.sleep(delay)
        raise TimeoutError(f"Cilium not ready after {retries*delay}s")

    # ---------- Hosts/inventory/labels/taints ----------

    def update_hosts_and_inventory(self, opts: SetupOptions) -> List[Tuple[str, str]]:
        entries = build_hosts_entries(self.mgmt_context, str(opts.workload_kubeconfig))

        update_hosts_file(
            entries=entries,
            hosts_file=opts.hosts_file,
            domain_suffix=opts.domain_suffix,
            cleanup_regex=r".*openstack-infra-(control-plane|workers)-.*\.net\.daalu\.io$",
        )

        # Render inventories (optional; requires jinja2)
        out_dir = (self.repo_root / opts.output_inventory_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        render_inventory_templates(
            entries=entries,
            templates_dir=self.repo_root / opts.templates_dir,
            output_hosts_ini=out_dir / "hosts.ini",
            output_openstack_hosts_ini=out_dir / "openstack_hosts.ini",
            extra_vars={
                "ansible_user": "builder",
                "ansible_password": "admin10",
                "ansible_become_password": "admin10",
            },
        )
        return entries

    def label_and_taint_nodes(self, opts: SetupOptions, entries: List[Tuple[str, str]]) -> None:
        # Set bootstrap labels and taints on each node
        for _, hostname in entries:
            # label
            subprocess.run([
                "kubectl", "--kubeconfig", str(opts.workload_kubeconfig),
                "label", "node", hostname,
                "node.cilium.io/agent-not-ready=true",
                "kubernetes.io/os=linux",
                "--overwrite",
            ], check=False)
            # taint
            subprocess.run([
                "kubectl", "--kubeconfig", str(opts.workload_kubeconfig),
                "taint", "node", hostname,
                "node.cilium.io/agent-not-ready=true:NoSchedule",
                "--overwrite",
            ], check=False)

    # ---------- One-shot orchestrator ----------

    #def run(self, opts: SetupOptions) -> None:
    #    self.generate_kubeconfig(opts)
    #    ip = self.get_control_plane_ip(opts)
    #    self.install_cilium(opts, ip)
    #    self.wait_for_cilium(opts)
    #    nodes = self.update_hosts_and_inventory(opts)
    #    self.label_and_taint_nodes(opts, nodes)

    def run(self, opts: SetupOptions) -> None:
        bus = EventBus([])  # optionally inject external observers later
        run_ctx = new_ctx(env="setup", context=self.mgmt_context or "default")

        bus.emit(SetupStarted(cluster_name=opts.cluster_name, **run_ctx))
        try:
            # 1) Generate workload kubeconfig
            self.generate_kubeconfig(opts)
            bus.emit(KubeconfigGenerated(cluster_name=opts.cluster_name, **run_ctx))

            # 2) Discover control-plane IP
            ip = self.get_control_plane_ip(opts)
            bus.emit(ControlPlaneDiscovered(cluster_name=opts.cluster_name, ip=ip, **run_ctx))

            # 3) Install Cilium
            self.install_cilium(opts, ip)
            bus.emit(CiliumInstalled(cluster_name=opts.cluster_name, ip=ip, **run_ctx))

            # 4) Wait for Cilium to be ready
            self.wait_for_cilium(opts)
            bus.emit(CiliumReady(cluster_name=opts.cluster_name, **run_ctx))

            # 5) Update /etc/hosts and inventories
            nodes = self.update_hosts_and_inventory(opts)
            bus.emit(HostsUpdated(cluster_name=opts.cluster_name, count=len(nodes), **run_ctx))

            # 6) Label and taint nodes
            self.label_and_taint_nodes(opts, nodes)
            bus.emit(NodesLabeled(cluster_name=opts.cluster_name, count=len(nodes), **run_ctx))

            bus.emit(SetupSummary(cluster_name=opts.cluster_name, status="OK", **run_ctx))
        except Exception as e:
            bus.emit(SetupFailed(cluster_name=opts.cluster_name, error=str(e), **run_ctx))
            bus.emit(SetupSummary(cluster_name=opts.cluster_name, status="FAILED", error=str(e), **run_ctx))
            raise