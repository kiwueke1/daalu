# src/daalu/cli/app.py
import os
from pathlib import Path
from typing import Optional, List

import typer
import yaml
import time

from daalu.hpc.cli import cli as hpc_cli
from daalu.config.loader import load_config
from daalu.helm.cli_runner import HelmCliRunner
from daalu.deploy.executor import deploy_all, DeployOptions
from daalu.observers.console import ConsoleObserver

from daalu.bootstrap.cluster_api_manager import ClusterAPIManager
from daalu.bootstrap.setup_manager import SetupManager, SetupOptions

# Node OS bootstrap (Paramiko)
from daalu.bootstrap.node.ssh_bootstrapper import SshBootstrapper
from daalu.bootstrap.node.models import Host, NodeBootstrapOptions

# Helpers moved to cli/helper.py
from daalu.cli.helper import (
    inventory_path,
    read_hosts_from_inventory,
    plan_from_tags,
    maybe_read_kubeconfig_text,
)
from daalu.bootstrap.ceph.manager import CephManager
from daalu.bootstrap.ceph.models import CephHost, CephConfig
from daalu.cli.helper import read_group_from_inventory
from daalu.observers.dispatcher import EventBus
from daalu.observers.console import ConsoleObserver

from daalu.bootstrap.metal3.cluster_api_manager import Metal3ClusterAPIManager
from daalu.utils.execution import ExecutionContext
from daalu.bootstrap.metal3.helpers import wait_for_control_plane_ready

from daalu.logging.log import init_logging
from daalu.observers.jsonfile import JsonFileObserver
from daalu.observers.logger import LoggerObserver
from daalu.observers.events import new_ctx


app = typer.Typer(help="Daalu Deployment CLI")
app.add_typer(hpc_cli, name="hpc")

# Resolve workspace root (used by bootstrap managers to find playbooks/)
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("WORKSPACE_ROOT", str(WORKSPACE_ROOT))


@app.command()
def deploy(
    config: str = typer.Argument(..., help="Workload cluster config (for Helm stage)"),
    context: Optional[str] = typer.Option(None, "--context", "-c"),
    mgmt_context: Optional[str] = typer.Option(None, "--mgmt-context"),
    cluster_name: str = typer.Option("openstack-infra", "--cluster-name"),
    cluster_namespace: str = typer.Option("default", "--cluster-namespace"),
    skip_clusterapi: bool = typer.Option(False, "--skip-clusterapi"),
    skip_setup: bool = typer.Option(False, "--skip-setup"),
    skip_nodes: bool = typer.Option(False, "--skip-nodes"),
    node_tags: Optional[str] = typer.Option(None, "--node-tags"),
    ssh_username: str = typer.Option("ubuntu", "--ssh-username"),
    ssh_password: Optional[str] = typer.Option(None, "--ssh-password"),
    ssh_key: Optional[Path] = typer.Option(None, "--ssh-key"),
    managed_user: str = typer.Option("kez", "--managed-user"),
    managed_user_password: str = typer.Option("admin10", "--managed-user-password"),
    domain_suffix: str = typer.Option("net.daalu.io", "--domain-suffix"),
    skip_ceph: bool = typer.Option(False, "--skip-ceph"),
    debug: bool = typer.Option(False, "--debug", "-d"),
    ceph_version: str = typer.Option("17.2.6", "--ceph-version"),
    ceph_image: Optional[str] = typer.Option(None, "--ceph-image"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    typer.echo(f"Workspace root: {WORKSPACE_ROOT}")
    cfg = load_config(config)

    observers = [ConsoleObserver()]

    # --- 1) Cluster API phase ---
    if not skip_clusterapi:
        typer.echo("\n[clusterapi] Bootstrapping Cluster API components...")

        cluster_name = cfg.cluster_api.cluster_name
        provider = getattr(cfg.cluster_api, "provider", "proxmox")

        if provider == "metal3":
            ctx = ExecutionContext(dry_run=dry_run)
            logger, run_id, log_path = init_logging()

            observers = [
                ConsoleObserver(),
                LoggerObserver(logger),
                JsonFileObserver(Path.home() / ".daalu/logs" / f"{run_id}.jsonl"),
            ]

            bus = EventBus(observers=[ConsoleObserver()])
            event_ctx = new_ctx(env=cfg.environment, context=mgmt_context)
            event_ctx["run_id"] = run_id

            mgr = Metal3ClusterAPIManager(
                workspace_root=WORKSPACE_ROOT,
                mgmt_context=mgmt_context,
                bus=bus,
                ctx=ctx,
            )

            image_ctx = mgr.prepare_images(cfg)
            paths = mgr.generate_templates(cfg)

            mgr.apply_cluster(paths, namespace=cfg.cluster_api.metal3_namespace)
            mgr.apply_controlplane(paths, namespace=cfg.cluster_api.metal3_namespace)
            mgr.apply_workers(paths, namespace=cfg.cluster_api.metal3_namespace)
            mgr.verify(cfg)

            if cfg.cluster_api.pivot:
                mgr.pivot(cfg)

        else:
            manager = ClusterAPIManager(
                WORKSPACE_ROOT,
                mgmt_context=mgmt_context,
                observers=observers,
            )
            manager.deploy_dynamic(cfg)

    # --- 3) Node OS bootstrap via SSH ---
    if not skip_nodes:
        typer.echo("\n[nodes] Bootstrapping node OS roles via SSH...")

        inv = inventory_path(WORKSPACE_ROOT)
        inventory_hosts = read_hosts_from_inventory(inv)

        if not inventory_hosts:
            typer.echo(f"[nodes] No hosts found in {inv}. Skipping node bootstrap.")
        else:
            hosts: List[Host] = []
            for host in inventory_hosts:
                typer.echo(f"[nodes] Host {host.hostname} @ {host.address}")
                typer.echo(f"        netplan: {'yes' if host.netplan_content else 'no'}")
                hosts.append(
                    Host(
                        hostname=host.hostname,
                        address=host.address,
                        netplan_content=host.netplan_content,
                        username=ssh_username,
                        password=None,  # key-only auth
                        pkey_path=ssh_key,
                        authorized_key_path=(
                            Path.home() / ".ssh" / "openstack-key.pub"
                            if (Path.home() / ".ssh" / "openstack-key.pub").exists()
                            else None
                        ),
                    )
                )  # ✅ FIX: closing hosts.append(

            plan = plan_from_tags(node_tags)
            kubeconfig_text = maybe_read_kubeconfig_text(f"/tmp/kubeconfig-{cfg.cluster_api.cluster_name}.yaml")
            kubeconfig_path = f"/tmp/kubeconfig-{cfg.cluster_api.cluster_name}.yaml"

            opts = NodeBootstrapOptions(
                cluster_name=cluster_name,
                kubeconfig_content=kubeconfig_text,
                domain_suffix=domain_suffix,
                managed_user=managed_user,
                managed_user_password_plain=managed_user_password,
            )

            SshBootstrapper().bootstrap(hosts, plan, opts)
            typer.echo("[nodes] Node OS bootstrap completed.")
    else:
        typer.echo("[nodes] Skipped.")

    # --- 4) Ceph phase ---
    if not skip_ceph:
        typer.echo("\n[ceph] Deploying Ceph...")

        inv = inventory_path(WORKSPACE_ROOT)
        ceph_pairs = read_group_from_inventory(inv, "ceph")

        if ceph_pairs:
            ceph_hosts = [
                CephHost(
                    hostname=h,
                    address=a,
                    username=managed_user,
                    pkey_path=str(ssh_key) if ssh_key else None,
                )
                for (h, a) in ceph_pairs
            ]

            ceph_cfg = CephConfig(
                version=ceph_version,
                image=ceph_image,
                apply_osds_all_devices=True,
            )

            CephManager(bus=EventBus(observers=[ConsoleObserver()])).deploy(
                ceph_hosts, ceph_cfg
            )

    # --- 5) Helm phase ---
    typer.echo("\n[helm] Starting Helm deployments...")
    helm = HelmCliRunner(kube_context=context or cfg.context, debug=debug)
    report = deploy_all(cfg, helm, options=DeployOptions(), observers=observers)

    typer.echo("\nDeployment summary:")
    typer.echo(report.summary())


if __name__ == "__main__":
    app()

