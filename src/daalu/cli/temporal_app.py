# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/cli/app.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, List, Set, Tuple

import typer
import paramiko

import asyncio

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

from daalu.temporal.models import DeployRequest
from daalu.cli.temporal_start import start_deploy_workflow

from daalu.deploy.steps import (
    resolve_install_plan,
    deploy_cluster_api_metal3,
    deploy_cluster_api_generic,
    deploy_nodes,
    connect_controller_ssh,
    deploy_ceph,
    deploy_csi,
    deploy_infrastructure,
)

# ------------------------------------------------------------------------------
# CLI setup
# ------------------------------------------------------------------------------

app = typer.Typer(help="Daalu Deployment CLI")
app.add_typer(hpc_cli, name="hpc")

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("WORKSPACE_ROOT", str(WORKSPACE_ROOT))



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
    temporal: bool = typer.Option(
        False, "--temporal", help="Run deployment as a Temporal workflow"
    ),
):
    typer.echo(f"Workspace root: {WORKSPACE_ROOT}")

    # ------------------------------------------------------------------
    # TEMPORAL PATH (early exit)
    # ------------------------------------------------------------------
    if temporal:
        req = DeployRequest(
            config_path=config,
            workspace_root=str(WORKSPACE_ROOT),
            install=install,
            infra=infra,
            context=context,
            mgmt_context=mgmt_context,
            cluster_name=cluster_name,
            cluster_namespace=cluster_namespace,
            node_tags=node_tags,
            ssh_username=ssh_username,
            ssh_password=ssh_password,
            ssh_key=str(ssh_key) if ssh_key else None,
            managed_user=managed_user,
            managed_user_password=managed_user_password,
            domain_suffix=domain_suffix,
            ceph_version=ceph_version,
            ceph_image=ceph_image,
            dry_run=dry_run,
            debug=debug,
        )

        workflow_id = asyncio.run(start_deploy_workflow(req))
        typer.echo(f"[temporal] Deployment workflow started: {workflow_id}")
        raise typer.Exit(0)

    # ------------------------------------------------------------------
    # EXISTING SYNCHRONOUS PATH (unchanged)
    # ------------------------------------------------------------------
    cfg = load_config(config)
    install_plan = resolve_install_plan(install)

    print(f"install plan is {install_plan}")

    # ------------------------------------------------------------------------------
    # 1) Cluster API
    # ------------------------------------------------------------------------------
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
    # Shared controller SSH (Ceph / CSI / Infra)
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
        elif "csi" in install_plan:
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

        # ------------------------------------------------------------------------------
        # 4) CSI
        # ------------------------------------------------------------------------------
        if "csi" in install_plan:
            deploy_csi(
                helm=helm,
                ceph_hosts=ceph_hosts,
                kubeconfig_path=kubeconfig_path,
            )

        # ------------------------------------------------------------------------------
        # 5) Infrastructure
        # ------------------------------------------------------------------------------
        if "infrastructure" in install_plan:
            deploy_infrastructure(
                helm=helm,
                ssh=ssh,
                workspace_root=WORKSPACE_ROOT,
                infra_flag=infra,
                kubeconfig_path=kubeconfig_path,
            )

    finally:
        if client:
            client.close()


@app.command()
def deploy_1(
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
    temporal: bool = typer.Option(False, "--temporal", help="Start deployment as a Temporal workflow"),
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
