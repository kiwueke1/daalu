# src/daalu/cli/app.py
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional, List, Set, Tuple

import typer
import paramiko

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
from daalu.bootstrap.monitoring.manager import MonitoringManager
from daalu.bootstrap.monitoring.registry import build_monitoring_components
from daalu.bootstrap.monitoring.models import parse_monitoring_flag
from daalu.bootstrap.shared.keycloak.models import KeycloakIAMConfig, KeycloakAdminAuth, KeycloakRealmSpec, KeycloakClientSpec
from daalu.bootstrap.openstack.models import parse_openstack_flag
from daalu.bootstrap.openstack.registry import build_openstack_components
from daalu.bootstrap.openstack.manager import OpenStackManager





# ------------------------------------------------------------------------------
# CLI setup
# ------------------------------------------------------------------------------

app = typer.Typer(help="Daalu Deployment CLI")

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
    "monitoring",
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
    Bootstrap nodes via SSH based on inventory + tags,
    then label nodes for OpenStack scheduling.
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
        cluster_namespace=cfg.cluster_api.namespace,
        kubeconfig_content=kubeconfig_text,
        domain_suffix=domain_suffix,
        managed_user=managed_user,
        managed_user_password_plain=managed_user_password,
    )

    SshBootstrapper().bootstrap(hosts, plan, opts)

    # ------------------------------------------------------------------
    # Label nodes so CSI / OpenStack components can schedule
    # ------------------------------------------------------------------
    typer.echo("\n[nodes] Labeling nodes...")

    kubeconfig_path = f"/tmp/kubeconfig-{cfg.cluster_api.cluster_name}.yaml"
    controllers = {h for h, _ in read_group_from_inventory(inv, "controllers")}
    computes = {h for h, _ in read_group_from_inventory(inv, "computes")}

    for node in controllers | computes:
        labels = ["openvswitch=enabled"]
        if node in controllers:
            labels.append("openstack-control-plane=enabled")
        if node in computes:
            labels.append("openstack-compute-node=enabled")

        subprocess.run(
            [
                "kubectl", "--kubeconfig", kubeconfig_path,
                "label", "node", node,
                *labels,
                "--overwrite",
            ],
            check=True,
        )
        typer.echo(f"  labeled {node}: {', '.join(labels)}")

        # Remove NoSchedule taint from control-plane nodes
        if node in controllers:
            subprocess.run(
                [
                    "kubectl", "--kubeconfig", kubeconfig_path,
                    "taint", "node", node,
                    "node-role.kubernetes.io/control-plane:NoSchedule-",
                ],
                check=False,  # may not exist
            )
            typer.echo(f"  removed control-plane NoSchedule taint from {node}")


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


def deploy_monitoring(
    *,
    cfg,
    helm: HelmCliRunner,
    ssh: SSHRunner,
    workspace_root: Path,
    infra_flag: Optional[str],
    kubeconfig_path: str,
) -> None:
    """
    Deploy monitoring components (e.g. node-feature-discovery).
    Uses --infra flag for component selection.
    """
    typer.echo("\n[monitoring] Installing monitoring components...")

    selection = parse_monitoring_flag(infra_flag)

    components = build_monitoring_components(
        selection=selection,
        workspace_root=workspace_root,
        kubeconfig_path=kubeconfig_path,
        cfg=cfg,
    )


    MonitoringManager(
        helm=helm,
        ssh=ssh,
    ).deploy(components)


def deploy_openstack(
    *,
    cfg,
    helm: HelmCliRunner,
    ssh: SSHRunner,
    workspace_root: Path,
    infra_flag: Optional[str],
    kubeconfig_path: str,
    phase: Optional[str] = None,
):
    typer.echo("\n[openstack] Installing OpenStack components...")
    if phase:
        typer.echo(f"[openstack] Running phase: {phase}")

    selection = parse_openstack_flag(infra_flag)

    components = build_openstack_components(
        cfg=cfg,
        selection=selection,
        workspace_root=workspace_root,
        kubeconfig_path=kubeconfig_path,
        ssh=ssh,
    )

    OpenStackManager(
        helm=helm,
        ssh=ssh,
    ).deploy(components, phase=phase)

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
    ceph_version: str = typer.Option("20.2.0", "--ceph-version"),
    ceph_image: Optional[str] = typer.Option(None, "--ceph-image"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    debug: bool = typer.Option(False, "--debug"),
    phase: Optional[str] = typer.Option(
        None,
        "--phase",
        help="Run only a specific deploy phase: pre_install, helm, or post_install",
    ),
):
    typer.echo(f"Workspace root: {WORKSPACE_ROOT}")

    logger, run_id, log_path = init_logging(verbose=debug)

    typer.echo("")
    typer.secho("Daalu Deployment Started", bold=True)
    typer.echo(f"  Run ID   : {run_id}")
    typer.echo(f"  Logs     : {log_path}")
    typer.echo("")


    #cfg = load_config(config)
    cfg: DaaluConfig = load_config(config)
    install_plan = resolve_install_plan(install)

    # ------------------------------------------------------------------------------
    # 1) Cluster API
    # ------------------------------------------------------------------------------
    logger.debug("install plan: %s", install_plan)

    if "cluster-api" in install_plan:
        typer.echo("\n[cluster-api] Installing Cluster API...")

        # Use CLI --ssh-key to derive ssh_public_key_path if not set in config
        if ssh_key and cfg.cluster_api and not str(cfg.cluster_api.ssh_public_key_path).strip("."):
            pub_key_path = Path(f"{ssh_key}.pub")
            if pub_key_path.expanduser().is_file():
                cfg.cluster_api.ssh_public_key_path = pub_key_path

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
        # Use image_username from cluster_api config if --ssh-username was not
        # explicitly provided (i.e. still the default "ubuntu").  Metal3 nodes
        # are provisioned with image_username, so we must SSH as that user.
        effective_ssh_user = ssh_username
        if ssh_username == "ubuntu" and cfg.cluster_api and getattr(cfg.cluster_api, "image_username", None):
            effective_ssh_user = cfg.cluster_api.image_username
            typer.echo(f"[nodes] Using image_username '{effective_ssh_user}' from cluster config for SSH")

        deploy_nodes(
            cfg=cfg,
            workspace_root=WORKSPACE_ROOT,
            cluster_name=cluster_name,
            node_tags=node_tags,
            ssh_username=effective_ssh_user,
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

        # Ensure helm is installed on the remote node
        typer.echo("[setup] Ensuring helm is installed on remote node...")
        rc, out, _ = ssh.run("which helm", sudo=False)
        if rc != 0:
            typer.echo("[setup] Helm not found, installing...")
            rc, out, err = ssh.run(
                "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash",
                sudo=True,
            )
            if rc != 0:
                raise RuntimeError(f"Failed to install helm on remote node: {err}")
            typer.echo("[setup] Helm installed successfully")
        else:
            typer.echo(f"[setup] Helm already installed at {out.strip()}")

        helm = HelmCliRunner(ssh=ssh, kube_context=context or cfg.context)

        kubeconfig_path = f"/tmp/kubeconfig-{cfg.cluster_api.cluster_name}.yaml"
        if os.path.isfile(kubeconfig_path):
            ssh.put_file(
                local_path=kubeconfig_path,
                remote_path=kubeconfig_path,
            )
        else:
            logger.warning(
                "Local kubeconfig %s not found – skipping upload", kubeconfig_path
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
        
        #------------------------------------------------------------------
        # 6) Monitoring
        #------------------------------------------------------------------
        if "monitoring" in install_plan:
            deploy_monitoring(
                cfg=cfg,
                helm=helm,
                ssh=ssh,
                workspace_root=WORKSPACE_ROOT,
                infra_flag=infra,
                kubeconfig_path=kubeconfig_path
            )
        # ------------------------------------------------------------------------------
        # 7) OpenStack
        # ------------------------------------------------------------------------------
        if "openstack" in install_plan:
            deploy_openstack(
                cfg=cfg,
                helm=helm,
                ssh=ssh,
                workspace_root=WORKSPACE_ROOT,
                infra_flag=infra,
                kubeconfig_path=kubeconfig_path,
                phase=phase,
            )

    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    app()
