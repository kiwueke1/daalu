# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/cli/app.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, List, Set, Tuple

import typer
import paramiko

from daalu.hpc.cli import cli as hpc_cli
from daalu.config.loader import load_config
from daalu.helm.cli_runner import HelmCliRunner

from daalu.bootstrap.cluster_api_manager import ClusterAPIManager
from daalu.bootstrap.metal3.cluster_api_manager import Metal3ClusterAPIManager
from daalu.bootstrap.setup_manager import SetupManager  # (kept; used in other flows)

from daalu.bootstrap.node.ssh_bootstrapper import SshBootstrapper
from daalu.bootstrap.node.models import Host, NodeBootstrapOptions

from daalu.bootstrap.ceph.manager import CephManager
from daalu.bootstrap.ceph.models import CephHost, CephConfig

from daalu.bootstrap.infrastructure.manager import InfrastructureManager
from daalu.bootstrap.infrastructure.registry import build_infrastructure_components
from daalu.bootstrap.infrastructure.models import parse_infra_flag

from daalu.bootstrap.csi.manager import CSIManager
from daalu.bootstrap.csi.models import CSIConfig

from daalu.cli.helper import (
    inventory_path,
    read_hosts_from_inventory,
    read_group_from_inventory,
    plan_from_tags,
    maybe_read_kubeconfig_text,
)

from daalu.utils.execution import ExecutionContext
from daalu.utils.ssh_runner import SSHRunner

from daalu.logging.log import init_logging
from daalu.observers.console import ConsoleObserver
from daalu.observers.dispatcher import EventBus
from daalu.observers.logger import LoggerObserver
from daalu.observers.jsonfile import JsonFileObserver
from daalu.observers.events import new_ctx, LifecycleEvent


# ------------------------------------------------------------------------------
# CLI setup
# ------------------------------------------------------------------------------

app = typer.Typer(help="Daalu Deployment CLI")
app.add_typer(hpc_cli, name="hpc")

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("WORKSPACE_ROOT", str(WORKSPACE_ROOT))


# ------------------------------------------------------------------------------
# Install targets
# ------------------------------------------------------------------------------

ALL_TARGETS: Set[str] = {
    "cluster-api",
    "nodes",
    "ceph",
    "csi",
    "infrastructure",
    "openstack",
}


def resolve_install_plan(install: Optional[str]) -> Set[str]:
    """
    Resolve install plan from --install flag.

    Rules:
    - No --install → install everything
    - --install all → install everything
    - Otherwise → install only specified targets
    """
    if not install:
        return set(ALL_TARGETS)

    items = {i.strip() for i in install.split(",") if i.strip()}
    if "all" in items:
        return set(ALL_TARGETS)

    unknown = items - ALL_TARGETS
    if unknown:
        raise typer.BadParameter(
            f"Unknown install targets: {', '.join(sorted(unknown))}\n"
            f"Valid targets: {', '.join(sorted(ALL_TARGETS))}"
        )

    return items


# ------------------------------------------------------------------------------
# Helpers (extracted logic)
# ------------------------------------------------------------------------------

def deploy_cluster_api_metal3(
    *,
    cfg,
    workspace_root: Path,
    mgmt_context: Optional[str],
    dry_run: bool,
) -> None:
    """
    Deploy Cluster API using Metal3 with structured lifecycle logging.
    """
    ctx = ExecutionContext(dry_run=dry_run)
    logger, run_id, _ = init_logging()

    observers = [
        ConsoleObserver(),
        LoggerObserver(logger),
        JsonFileObserver(Path.home() / ".daalu/logs" / f"{run_id}.jsonl"),
    ]

    bus = EventBus(observers=observers)

    event_ctx = new_ctx(env=cfg.environment, context=mgmt_context)
    event_ctx.update(
        {
            "run_id": run_id,
            "component": "cluster-api",
            "provider": "metal3",
            "cluster": getattr(cfg.cluster_api, "cluster_name", None),
            "namespace": getattr(cfg.cluster_api, "metal3_namespace", None),
        }
    )

    bus.emit(
        LifecycleEvent(
            "metal3.cluster_api.run",
            "START",
            "Starting Metal3 Cluster API workflow",
        )
    )

    try:
        mgr = Metal3ClusterAPIManager(
            workspace_root=workspace_root,
            mgmt_context=mgmt_context,
            bus=bus,
            ctx=ctx,
        )

        paths = mgr.generate_templates(cfg)
        mgr.apply_cluster(paths, namespace=cfg.cluster_api.metal3_namespace)
        mgr.apply_controlplane(paths, namespace=cfg.cluster_api.metal3_namespace)
        mgr.apply_workers(paths, namespace=cfg.cluster_api.metal3_namespace)
        mgr.verify(cfg)

        if getattr(cfg.cluster_api, "pivot", False):
            mgr.pivot(cfg)

        bus.emit(
            LifecycleEvent(
                "metal3.cluster_api.run",
                "SUCCESS",
                "Metal3 Cluster API workflow completed",
            )
        )
    except Exception as exc:
        bus.emit(
            LifecycleEvent(
                "metal3.cluster_api.run",
                "FAILURE",
                f"Metal3 Cluster API workflow failed: {exc}",
            )
        )
        raise


def deploy_cluster_api_generic(
    *,
    cfg,
    workspace_root: Path,
    mgmt_context: Optional[str],
) -> None:
    """
    Deploy Cluster API using the generic (non-metal3) manager.
    """
    observers = [ConsoleObserver()]
    ClusterAPIManager(
        workspace_root,
        mgmt_context=mgmt_context,
        observers=observers,
    ).deploy_dynamic(cfg)


def deploy_nodes(
    *,
    cfg,
    workspace_root: Path,
    cluster_name: str,
    node_tags: Optional[str],
    ssh_username: str,
    ssh_key: Optional[Path],
    domain_suffix: str,
    managed_user: str,
    managed_user_password: str,
) -> None:
    """
    Bootstrap nodes via SSH based on inventory + tags.
    """
    typer.echo("\n[nodes] Bootstrapping nodes...")

    inv = inventory_path(workspace_root)
    inventory_hosts = read_hosts_from_inventory(inv)

    hosts: List[Host] = [
        Host(
            hostname=h.hostname,
            address=h.address,
            netplan_content=h.netplan_content,
            username=ssh_username,
            pkey_path=ssh_key,
        )
        for h in inventory_hosts
    ]

    plan = plan_from_tags(node_tags)
    kubeconfig_text = maybe_read_kubeconfig_text(
        f"/tmp/kubeconfig-{cfg.cluster_api.cluster_name}.yaml"
    )

    opts = NodeBootstrapOptions(
        cluster_name=cluster_name,
        kubeconfig_content=kubeconfig_text,
        domain_suffix=domain_suffix,
        managed_user=managed_user,
        managed_user_password_plain=managed_user_password,
    )

    SshBootstrapper().bootstrap(hosts, plan, opts)


def connect_controller_ssh(
    *,
    workspace_root: Path,
    managed_user: str,
    ssh_key: Optional[Path],
    ssh_password: Optional[str],
) -> Tuple[paramiko.SSHClient, Host]:
    """
    Connect to the first controller from inventory and return (client, controller_host).
    """
    inv = inventory_path(workspace_root)
    controller_pairs = read_group_from_inventory(inv, "controllers")
    if not controller_pairs:
        raise typer.Exit("No controllers found in inventory")

    controller_host = Host(
        hostname=controller_pairs[0][0],
        address=controller_pairs[0][1],
        username=managed_user,
        pkey_path=str(ssh_key) if ssh_key else None,
    )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    client.connect(
        hostname=controller_host.address,
        username=controller_host.username,
        key_filename=str(ssh_key) if ssh_key else None,
        password=ssh_password,
    )

    return client, controller_host


def deploy_ceph(
    *,
    workspace_root: Path,
    managed_user: str,
    ssh_key: Optional[Path],
    ceph_version: str,
    ceph_image: Optional[str],
) -> List[CephHost]:
    """
    Deploy Ceph and return the resolved Ceph host list (used by CSI).
    """
    typer.echo("\n[ceph] Installing Ceph...")

    inv = inventory_path(workspace_root)
    ceph_pairs = read_group_from_inventory(inv, "ceph")
    ceph_hosts: List[CephHost] = [
        CephHost(
            hostname=h,
            address=a,
            username=managed_user,
            pkey_path=str(ssh_key) if ssh_key else None,
        )
        for h, a in ceph_pairs
    ]

    CephManager(
        bus=EventBus(observers=[ConsoleObserver()])
    ).deploy(
        ceph_hosts,
        CephConfig(
            version=ceph_version,
            image=ceph_image,
            apply_osds_all_devices=True,
        ),
    )

    return ceph_hosts


def deploy_csi(
    *,
    helm: HelmCliRunner,
    ceph_hosts: List[CephHost],
    kubeconfig_path: str,
) -> None:
    """
    Deploy CSI (RBD) using Ceph hosts.
    """
    typer.echo("\n[csi] Installing CSI...")

    CSIManager(
        bus=EventBus(observers=[ConsoleObserver()]),
        helm=helm,
        ceph_hosts=ceph_hosts,
    ).deploy(
        CSIConfig(
            driver="rbd",
            kubeconfig_path=kubeconfig_path,
        )
    )


def deploy_infrastructure(
    *,
    helm: HelmCliRunner,
    ssh: SSHRunner,
    workspace_root: Path,
    infra_flag: Optional[str],
    kubeconfig_path: str,
) -> None:
    """
    Deploy infra components (e.g. metallb, argocd, jenkins, etc.)
    """
    typer.echo("\n[infrastructure] Installing infrastructure components...")

    selection = parse_infra_flag(infra_flag)

    components = build_infrastructure_components(
        selection=selection,
        workspace_root=workspace_root,
        kubeconfig_path=kubeconfig_path,
    )

    InfrastructureManager(
        helm=helm,
        ssh=ssh,
    ).deploy(components)


# ------------------------------------------------------------------------------
# Deploy command
# ------------------------------------------------------------------------------

@app.command()
def deploy(
    config: str = typer.Argument(..., help="Cluster definition YAML"),
    install: Optional[str] = typer.Option(
        None,
        "--install",
        help="Components to install: cluster-api,nodes,ceph,csi,infrastructure or all",
    ),
    infra: Optional[str] = typer.Option(
        None,
        "--infra",
        help="Infrastructure components (e.g. metallb,argocd or all)",
    ),
    context: Optional[str] = typer.Option(None, "--context"),
    mgmt_context: Optional[str] = typer.Option(None, "--mgmt-context"),
    cluster_name: str = typer.Option("openstack-infra", "--cluster-name"),
    cluster_namespace: str = typer.Option("default", "--cluster-namespace"),
    node_tags: Optional[str] = typer.Option(None, "--node-tags"),
    ssh_username: str = typer.Option("ubuntu", "--ssh-username"),
    ssh_password: Optional[str] = typer.Option(None, "--ssh-password"),
    ssh_key: Optional[Path] = typer.Option(None, "--ssh-key"),
    managed_user: str = typer.Option(..., "--managed-user", help="SSH user to create on nodes"),
    managed_user_password: str = typer.Option(..., "--managed-user-password", help="Password for managed user"),
    domain_suffix: str = typer.Option("net.daalu.io", "--domain-suffix"),
    ceph_version: str = typer.Option("17.2.6", "--ceph-version"),
    ceph_image: Optional[str] = typer.Option(None, "--ceph-image"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    debug: bool = typer.Option(False, "--debug"),
):
    typer.echo(f"Workspace root: {WORKSPACE_ROOT}")

    cfg = load_config(config)
    install_plan = resolve_install_plan(install)

    # ------------------------------------------------------------------------------
    # 1) Cluster API
    # ------------------------------------------------------------------------------
    print(f"install plan is {install_plan}")

    if "cluster-api" in install_plan:
        typer.echo("\n[cluster-api] Installing Cluster API...")

        provider = getattr(cfg.cluster_api, "provider", "proxmox")

        if provider == "metal3":
            deploy_cluster_api_metal3(
                cfg=cfg,
                workspace_root=WORKSPACE_ROOT,
                mgmt_context=mgmt_context,
                dry_run=dry_run,
            )
        else:
            deploy_cluster_api_generic(
                cfg=cfg,
                workspace_root=WORKSPACE_ROOT,
                mgmt_context=mgmt_context,
            )

    # ------------------------------------------------------------------------------
    # 2) Node bootstrap
    # ------------------------------------------------------------------------------
    if "nodes" in install_plan:
        deploy_nodes(
            cfg=cfg,
            workspace_root=WORKSPACE_ROOT,
            cluster_name=cluster_name,
            node_tags=node_tags,
            ssh_username=ssh_username,
            ssh_key=ssh_key,
            domain_suffix=domain_suffix,
            managed_user=managed_user,
            managed_user_password=managed_user_password,
        )

    # ------------------------------------------------------------------------------
    # Shared controller SSH (for Ceph/CSI/Infra)
    # ------------------------------------------------------------------------------
    client: Optional[paramiko.SSHClient] = None
    ceph_hosts: List[CephHost] = []

    try:
        client, _controller_host = connect_controller_ssh(
            workspace_root=WORKSPACE_ROOT,
            managed_user=managed_user,
            ssh_key=ssh_key,
            ssh_password=ssh_password,
        )

        ssh = SSHRunner(client)
        helm = HelmCliRunner(ssh=ssh, kube_context=context or cfg.context)

        kubeconfig_path = f"/tmp/kubeconfig-{cfg.cluster_api.cluster_name}.yaml"
        ssh.put_file(
            local_path=kubeconfig_path,
            remote_path=kubeconfig_path,
        )


        # ------------------------------------------------------------------------------
        # 3) Ceph
        # ------------------------------------------------------------------------------
        if "ceph" in install_plan:
            ceph_hosts = deploy_ceph(
                workspace_root=WORKSPACE_ROOT,
                managed_user=managed_user,
                ssh_key=ssh_key,
                ceph_version=ceph_version,
                ceph_image=ceph_image,
            )
        else:
            # If CSI is requested but Ceph wasn't installed in this run,
            # still resolve ceph hosts from inventory so CSIManager has them.
            if "csi" in install_plan:
                inv = inventory_path(WORKSPACE_ROOT)
                ceph_pairs = read_group_from_inventory(inv, "ceph")
                ceph_hosts = [
                    CephHost(
                        hostname=h,
                        address=a,
                        username=managed_user,
                        pkey_path=str(ssh_key) if ssh_key else None,
                    )
                    for h, a in ceph_pairs
                ]

        # ---------------------------------------------------------------------------
        # 4) CSI
        # ---------------------------------------------------------------------------
        if "csi" in install_plan:
            deploy_csi(
                helm=helm,
                ceph_hosts=ceph_hosts,
                kubeconfig_path=kubeconfig_path,
            )

        # ------------------------------------------------------------------------------
        # 5) Infrastructure
        #-----------------------------------------------------------------------------
        if "infrastructure" in install_plan:
            deploy_infrastructure(
                helm=helm,
                ssh=ssh,
                workspace_root=WORKSPACE_ROOT,
                infra_flag=infra,
                kubeconfig_path=kubeconfig_path,
            )

    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    app()
