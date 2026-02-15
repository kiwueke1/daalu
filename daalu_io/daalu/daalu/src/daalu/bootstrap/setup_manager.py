from __future__ import annotations

import logging
import os
import shutil
import re
import typer
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

from daalu.execution.runner import CommandRunner
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

log = logging.getLogger("daalu")

CILIUM_REPO = RepoSpec(name="cilium", url="https://helm.cilium.io/")


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
    Python replacement for Ansible-based setup.
    """

    def __init__(self, repo_root: Path, mgmt_context: Optional[str] = None, ctx=None):
        self.repo_root = repo_root
        self.mgmt_context = mgmt_context
        self.ctx = ctx

        self.runner = CommandRunner(
            logger=getattr(ctx, "logger", None),
            dry_run=getattr(ctx, "dry_run", False),
        )

    # ------------------------------------------------------------------
    # kubeconfig / control-plane IP
    # ------------------------------------------------------------------

    def generate_kubeconfig(self, opts: SetupOptions) -> None:
        opts.workload_kubeconfig.parent.mkdir(parents=True, exist_ok=True)

        result = self.runner.run(
            [
                "clusterctl",
                "get",
                "kubeconfig",
                opts.cluster_name,
            ],
            capture_output=True,
            check=True,
        )

        opts.workload_kubeconfig.write_text(result.stdout or "")

        try:
            opts.admin_conf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(opts.workload_kubeconfig, opts.admin_conf)
        except PermissionError:
            pass

    def get_control_plane_ip(self, opts: SetupOptions) -> str:
        result = self.runner.run(
            [
                "kubectl",
                "--kubeconfig",
                str(opts.workload_kubeconfig),
                "get",
                "nodes",
                "-l",
                "node-role.kubernetes.io/control-plane",
                "-o",
                "json",
            ],
            capture_output=True,
            check=True,
        )

        import json

        data = json.loads(result.stdout or "{}")
        items = data.get("items", [])

        if not items:
            raise RuntimeError("No control-plane nodes found")

        for addr in items[0].get("status", {}).get("addresses", []):
            if addr.get("type") == "InternalIP":
                return addr["address"]

        raise RuntimeError("No InternalIP found for control-plane node")

    # ------------------------------------------------------------------
    # Cilium via Helm
    # ------------------------------------------------------------------

    def install_cilium_test(self, opts: SetupOptions, api_host: str, api_port: int) -> None:
        os.environ["KUBECONFIG"] = str(opts.workload_kubeconfig)

        helm = HelmCliRunner(kube_context=None)

        helm.add_repo(CILIUM_REPO)
        helm.update_repos()

        values = {
            "ipam": {"mode": "kubernetes"},
            "kubeProxyReplacement": True,

            # 🔑 THIS IS THE FIX
            "k8sServiceHost": "10.10.0.249",
            #"k8sServiceHost": api_host,
            "k8sServicePort": api_port,

            "hostServices": {"enabled": True},
            "externalIPs": {"enabled": True},
            "nodePort": {"enabled": True},
            "hostPort": {"enabled": True},

            "image": {"pullPolicy": "IfNotPresent"},
            "operator": {"replicas": 1},

            "prometheus": {"enabled": True},
            "hubble": {
                "enabled": True,
                "relay": {"enabled": True},
                "ui": {"enabled": True},
            },
        }
        log.debug(f'values for cilium is {values}')

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

        helm.upgrade_install(rel)

    def install_cilium_1(self, opts: SetupOptions, control_plane_ip: str) -> None:
        os.environ["KUBECONFIG"] = str(opts.workload_kubeconfig)

        helm = HelmCliRunner(kube_context=None)

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
            "hubble": {
                "enabled": True,
                "relay": {"enabled": True},
                "ui": {"enabled": True},
            },
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

        helm.upgrade_install(rel)

    def wait_for_cilium(self, opts: SetupOptions, retries: int = 30, delay: int = 10) -> None:
        import json

        for _ in range(retries):
            result = self.runner.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(opts.workload_kubeconfig),
                    "get",
                    "pods",
                    "-n",
                    "kube-system",
                    "-l",
                    "k8s-app=cilium",
                    "-o",
                    "json",
                ],
                capture_output=True,
                check=True,
            )

            data = json.loads(result.stdout or "{}")
            pods = data.get("items", [])

            ready = 0
            for pod in pods:
                if pod.get("status", {}).get("phase") != "Running":
                    continue
                statuses = pod.get("status", {}).get("containerStatuses", []) or []
                if statuses and all(c.get("ready") for c in statuses):
                    ready += 1

            if ready >= opts.expected_cilium_pods:
                return

            time.sleep(delay)

        raise TimeoutError(f"Cilium not ready after {retries * delay}s")

    # ------------------------------------------------------------------
    # Hosts / inventory / labels / taints
    # ------------------------------------------------------------------

    def update_hosts_and_inventory(self, opts: SetupOptions) -> List[Tuple[str, str]]:
        entries = build_hosts_entries(self.mgmt_context, str(opts.workload_kubeconfig))

        update_hosts_file(
            entries=entries,
            hosts_file=opts.hosts_file,
            domain_suffix=opts.domain_suffix,
            cleanup_regex=r".*openstack-infra-(control-plane|workers)-.*\.net\.daalu\.io$",
        )

        out_dir = self.repo_root / opts.output_inventory_dir
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
        for _, hostname in entries:
            self.runner.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(opts.workload_kubeconfig),
                    "label",
                    "node",
                    hostname,
                    "node.cilium.io/agent-not-ready=true",
                    "kubernetes.io/os=linux",
                    "--overwrite",
                ],
                check=False,
            )

            self.runner.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(opts.workload_kubeconfig),
                    "taint",
                    "node",
                    hostname,
                    "node.cilium.io/agent-not-ready=true:NoSchedule",
                    "--overwrite",
                ],
                check=False,
            )

    def get_api_endpoint_from_kubeconfig(self, opts: SetupOptions) -> tuple[str, int]:
        """
        Extract API server host/port from workload kubeconfig.
        This must be the control-plane VIP (not node InternalIP).
        """
        import yaml
        import re

        cfg = yaml.safe_load(opts.workload_kubeconfig.read_text())
        server = cfg["clusters"][0]["cluster"]["server"]
        # e.g. https://10.10.0.249:6443

        m = re.match(r"^https?://([^:/]+)(?::(\d+))?$", server)
        if not m:
            raise RuntimeError(f"Unexpected kubeconfig server format: {server}")

        host = m.group(1)
        port = int(m.group(2) or 6443)

        return host, port


    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------

    def run(self, opts: SetupOptions) -> None:
        bus = EventBus([])
        run_ctx = new_ctx(env="setup", context=self.mgmt_context or "default")

        bus.emit(SetupStarted(cluster_name=opts.cluster_name, **run_ctx))

        try:
            # 1. Generate kubeconfig
            self.generate_kubeconfig(opts)
            bus.emit(KubeconfigGenerated(cluster_name=opts.cluster_name, **run_ctx))

            # 2. Discover API endpoint FROM KUBECONFIG (VIP)
            api_host, api_port = self.get_api_endpoint_from_kubeconfig(opts)
            bus.emit(
                ControlPlaneDiscovered(
                    cluster_name=opts.cluster_name,
                    ip=api_host,
                    **run_ctx,
                )
            )

            # 3. Install Cilium using VIP
            log.debug("starting cilium install from run method")
            self.install_cilium(opts, api_host, api_port)
            bus.emit(
                CiliumInstalled(
                    cluster_name=opts.cluster_name,
                    ip=api_host,
                    **run_ctx,
                )
            )

            # 4. Wait for Cilium
            self.wait_for_cilium(opts)
            bus.emit(CiliumReady(cluster_name=opts.cluster_name, **run_ctx))

            # 5. Hosts + inventory
            nodes = self.update_hosts_and_inventory(opts)
            bus.emit(HostsUpdated(cluster_name=opts.cluster_name, count=len(nodes), **run_ctx))

            # 6. Labels / taints
            self.label_and_taint_nodes(opts, nodes)
            bus.emit(NodesLabeled(cluster_name=opts.cluster_name, count=len(nodes), **run_ctx))

            bus.emit(SetupSummary(cluster_name=opts.cluster_name, status="OK", **run_ctx))

        except Exception as e:
            bus.emit(SetupFailed(cluster_name=opts.cluster_name, error=str(e), **run_ctx))
            bus.emit(SetupSummary(cluster_name=opts.cluster_name, status="FAILED", error=str(e), **run_ctx))
            raise


    def run_1(self, opts: SetupOptions) -> None:
        bus = EventBus([])
        run_ctx = new_ctx(env="setup", context=self.mgmt_context or "default")

        bus.emit(SetupStarted(cluster_name=opts.cluster_name, **run_ctx))

        try:
            self.generate_kubeconfig(opts)
            bus.emit(KubeconfigGenerated(cluster_name=opts.cluster_name, **run_ctx))

            ip = self.get_control_plane_ip(opts)
            bus.emit(ControlPlaneDiscovered(cluster_name=opts.cluster_name, ip=ip, **run_ctx))

            self.install_cilium(opts, ip)
            bus.emit(CiliumInstalled(cluster_name=opts.cluster_name, ip=ip, **run_ctx))

            self.wait_for_cilium(opts)
            bus.emit(CiliumReady(cluster_name=opts.cluster_name, **run_ctx))

            nodes = self.update_hosts_and_inventory(opts)
            bus.emit(HostsUpdated(cluster_name=opts.cluster_name, count=len(nodes), **run_ctx))

            self.label_and_taint_nodes(opts, nodes)
            bus.emit(NodesLabeled(cluster_name=opts.cluster_name, count=len(nodes), **run_ctx))

            bus.emit(SetupSummary(cluster_name=opts.cluster_name, status="OK", **run_ctx))

        except Exception as e:
            bus.emit(SetupFailed(cluster_name=opts.cluster_name, error=str(e), **run_ctx))
            bus.emit(SetupSummary(cluster_name=opts.cluster_name, status="FAILED", error=str(e), **run_ctx))
            raise
