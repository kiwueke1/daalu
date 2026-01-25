# src/daalu/deploy/steps.py

# src/daalu/deploy/steps.py
from __future__ import annotations

from pathlib import Path
from typing import Optional, List, Set, Tuple
import paramiko

from daalu.config.loader import load_config
from daalu.bootstrap.cluster_api_manager import ClusterAPIManager
from daalu.bootstrap.metal3.cluster_api_manager import Metal3ClusterAPIManager
from daalu.bootstrap.node.ssh_bootstrapper import SshBootstrapper
from daalu.bootstrap.node.models import Host, NodeBootstrapOptions
from daalu.bootstrap.ceph.manager import CephManager
from daalu.bootstrap.ceph.models import CephHost, CephConfig
from daalu.bootstrap.infrastructure.manager import InfrastructureManager
from daalu.bootstrap.infrastructure.registry import build_infrastructure_components
from daalu.bootstrap.infrastructure.models import parse_infra_flag
from daalu.bootstrap.csi.manager import CSIManager
from daalu.bootstrap.csi.models import CSIConfig
from daalu.helm.cli_runner import HelmCliRunner
from daalu.utils.ssh_runner import SSHRunner
from daalu.utils.execution import ExecutionContext
from daalu.logging.log import init_logging
from daalu.observers.console import ConsoleObserver
from daalu.observers.dispatcher import EventBus
from daalu.observers.logger import LoggerObserver
from daalu.observers.jsonfile import JsonFileObserver
from daalu.observers.events import new_ctx, LifecycleEvent
from daalu.cli.helper import (
    inventory_path,
    read_hosts_from_inventory,
    read_group_from_inventory,
    plan_from_tags,
    maybe_read_kubeconfig_text,
)



ALL_TARGETS: Set[str] = {
    "cluster-api",
    "nodes",
    "ceph",
    "csi",
    "infrastructure",
    "openstack",
}

def resolve_install_plan(install: Optional[str]) -> Set[str]:
    if not install:
        return set(ALL_TARGETS)
    items = {i.strip() for i in install.split(",") if i.strip()}
    if "all" in items:
        return set(ALL_TARGETS)
    unknown = items - ALL_TARGETS
    if unknown:
        raise ValueError(f"Unknown install targets: {unknown}")
    return items

# ---- all deploy_* functions live here unchanged ----

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

