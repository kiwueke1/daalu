# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/cli/app.py
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional, List, Set, Tuple

import typer
import paramiko

from daalu.config.loader import load_config
from daalu.config.models import DaaluConfig
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
from daalu.bootstrap.registry.manager import RegistryManager
from daalu.bootstrap.mgmt.manager import MgmtClusterManager
from daalu.bootstrap.mgmt.cleaner import MgmtClusterCleaner





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
    - --install none → install nothing (useful when only --install-local-registry is wanted)
    - Otherwise → install only specified targets
    """
    if not install:
        return set(ALL_TARGETS)

    items = {i.strip() for i in install.split(",") if i.strip()}
    if "all" in items:
        return set(ALL_TARGETS)
    if "none" in items:
        return set()

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
        insecure_registries=cfg.insecure_registries,
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
    cfg: Optional[DaaluConfig] = None,
) -> List[CephHost]:
    """
    Deploy Ceph and return the resolved Ceph host list (used by CSI).

    In-cluster Ceph hosts are read from the inventory [ceph] group.
    Additional dedicated storage hosts (not part of the K8s cluster) are
    taken from cfg.ceph.additional_ceph_hosts and appended with their
    explicit OSD device lists.
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

    # Append dedicated storage hosts from cluster config
    if cfg and cfg.ceph and cfg.ceph.additional_ceph_hosts:
        for ext in cfg.ceph.additional_ceph_hosts:
            ceph_hosts.append(
                CephHost(
                    hostname=ext.hostname,
                    address=ext.address,
                    username=ext.username,
                    port=ext.port,
                    password=ext.password or None,
                    pkey_path=ext.pkey_path or None,
                    osd_devices=ext.osd_devices,
                    is_mon_host=False,  # dedicated storage host — no mon/mgr
                )
            )
            typer.echo(
                f"[ceph] External host: {ext.hostname} ({ext.address}) "
                f"with {len(ext.osd_devices)} OSD device(s)"
            )

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


def _verify_and_get_registry_url(cfg, mgmt_kubeconfig: Optional[str]) -> str:
    """
    Verify that Harbor is already deployed on the mgmt cluster and return its URL.

    Called when --local-registry is used without --install-local-registry.
    Exits with an informative error if Harbor is not found.
    """
    import json
    import subprocess
    from daalu.config.models import RegistryConfig

    registry_cfg = cfg.registry or RegistryConfig()
    effective_kubeconfig = mgmt_kubeconfig or getattr(registry_cfg, "mgmt_kubeconfig", None)

    if not effective_kubeconfig:
        typer.secho(
            "[registry] ERROR: --local-registry requires --mgmt-kubeconfig or "
            "registry.mgmt_kubeconfig to be set in cluster.yaml.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)

    result = subprocess.run(
        [
            "helm", "--kubeconfig", effective_kubeconfig,
            "list", "-n", registry_cfg.harbor_namespace,
            "--filter", "^harbor$", "-o", "json",
        ],
        capture_output=True, text=True, check=False,
    )
    deployed = False
    if result.returncode == 0:
        try:
            releases = json.loads(result.stdout)
            deployed = any(
                r.get("name") == "harbor" and r.get("status") == "deployed"
                for r in releases
            )
        except Exception:
            pass

    if not deployed:
        typer.secho(
            "[registry] ERROR: --local-registry was specified but the Harbor registry "
            "does not appear to be deployed on the mgmt cluster "
            f"(namespace: {registry_cfg.harbor_namespace}). "
            "Deploy it first with --install-local-registry, or remove --local-registry "
            "if you do not need local registry image pulls.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)

    # Prefer explicit harbor_node_ip (provisioning NIC) over auto-detected InternalIP
    node_ip = getattr(registry_cfg, "harbor_node_ip", None)
    if not node_ip:
        node_result = subprocess.run(
            [
                "kubectl", "--kubeconfig", effective_kubeconfig,
                "get", "nodes",
                "-o", "jsonpath={.items[0].status.addresses[?(@.type==\"InternalIP\")].address}",
            ],
            capture_output=True, text=True, check=False,
        )
        node_ip = node_result.stdout.strip() or registry_cfg.harbor_hostname

    port_result = subprocess.run(
        [
            "kubectl", "--kubeconfig", effective_kubeconfig,
            "get", "svc", "harbor", "-n", registry_cfg.harbor_namespace,
            "-o", "jsonpath={.spec.ports[?(@.port==443)].nodePort}",
        ],
        capture_output=True, text=True, check=False,
    )
    nodeport = port_result.stdout.strip() or "30003"

    url = f"{node_ip}:{nodeport}"
    typer.echo(f"[registry] Using existing local registry at https://{url}")
    return url


def deploy_registry(
    *,
    cfg,
    workspace_root: Path,
    mgmt_kubeconfig: Optional[str],
    ssh_key: Optional[Path] = "~/.ssh/openstack-key",
    ssh_username: str = "kez",
    cluster_kubeconfig: Optional[str] = None,
) -> str:
    """
    Deploy Harbor registry on the mgmt cluster and mirror all images from assets/.

    If cluster_kubeconfig is provided, also configures containerd on all infra
    cluster nodes to trust Harbor's self-signed certificate (skip_verify).

    Returns the Harbor registry URL.
    """
    from daalu.config.models import RegistryConfig

    # Use registry block from cluster.yaml/secrets.yaml merge; fall back to defaults if absent.
    registry_cfg = cfg.registry or RegistryConfig()

    typer.echo("\n[registry] Deploying Harbor container registry...")

    mgr = RegistryManager(
        registry_cfg=registry_cfg,
        workspace_root=workspace_root,
        secrets_path=workspace_root / "cloud-config" / "secrets.yaml",  # kept for API compat
    )

    effective_kubeconfig = mgmt_kubeconfig or registry_cfg.mgmt_kubeconfig
    mgr.deploy_harbor(
        mgmt_kubeconfig=effective_kubeconfig,
        ssh_key=str(Path(ssh_key).expanduser()) if ssh_key else None,
        ssh_username=ssh_username,
        cluster_kubeconfig=cluster_kubeconfig,
    )
    mgr.mirror_images()

    url = mgr.harbor_registry_url()
    typer.echo(f"\n[registry] Harbor available at https://{url}")
    return url


def deploy_infrastructure(
    *,
    helm: HelmCliRunner,
    ssh: SSHRunner,
    workspace_root: Path,
    infra_flag: Optional[str],
    kubeconfig_path: str,
    keycloak_admin_password: str = "",
    registry_url: Optional[str] = None,
    registry_project: str = "openstack",
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
        keycloak_admin_password=keycloak_admin_password,
        registry_url=registry_url,
        registry_project=registry_project,
    )

    InfrastructureManager(
        helm=helm,
        ssh=ssh,
        registry_url=registry_url,
        registry_project=registry_project,
    ).deploy(components)


def deploy_monitoring(
    *,
    cfg,
    helm: HelmCliRunner,
    ssh: SSHRunner,
    workspace_root: Path,
    infra_flag: Optional[str],
    kubeconfig_path: str,
    registry_url: Optional[str] = None,
    registry_project: str = "openstack",
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
        registry_url=registry_url,
        registry_project=registry_project,
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
    managed_user: str = "builder",
    ssh_key: Optional[Path] = None,
    ssh_password: Optional[str] = None,
    registry_url: Optional[str] = None,
    registry_project: str = "openstack",
):
    typer.echo("\n[openstack] Installing OpenStack components...")
    if phase:
        typer.echo(f"[openstack] Running phase: {phase}")

    selection = parse_openstack_flag(infra_flag)

    # Build a separate SSH connection for the first Ceph node (if available).
    # rook-ceph-cluster needs to run cephadm on the node where Ceph is installed,
    # which is typically a worker/ceph node — not the controller.
    ceph_ssh = None
    inv = inventory_path(workspace_root)
    ceph_pairs = read_group_from_inventory(inv, "ceph")
    if ceph_pairs:
        ceph_addr = ceph_pairs[0][1]
        typer.echo(f"[openstack] Connecting to Ceph node {ceph_pairs[0][0]} ({ceph_addr})...")
        ceph_client = paramiko.SSHClient()
        ceph_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ceph_client.connect(
            hostname=ceph_addr,
            username=managed_user,
            key_filename=str(ssh_key) if ssh_key else None,
            password=ssh_password,
        )
        ceph_ssh = SSHRunner(ceph_client)

    try:
        # Build Host objects for all nodes so OpenvSwitchComponent.post_install
        # can open per-node SSH connections for the provisioning bridge migration.
        inv = inventory_path(workspace_root)
        _inv_hosts = read_hosts_from_inventory(inv)
        _node_hosts = [
            Host(
                hostname=h.hostname,
                address=h.address,
                username=managed_user,
                pkey_path=ssh_key,
            )
            for h in _inv_hosts
        ]

        components = build_openstack_components(
            cfg=cfg,
            selection=selection,
            workspace_root=workspace_root,
            kubeconfig_path=kubeconfig_path,
            ssh=ssh,
            ceph_ssh=ceph_ssh,
            node_hosts=_node_hosts,
        )

        OpenStackManager(
            helm=helm,
            ssh=ssh,
            registry_url=registry_url,
            registry_project=registry_project,
        ).deploy(components, phase=phase)
    finally:
        if ceph_ssh is not None:
            ceph_client.close()

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
    install_local_registry: bool = typer.Option(
        False,
        "--install-local-registry",
        help="Deploy Harbor registry on mgmt cluster and mirror images before installing components",
    ),
    local_registry: bool = typer.Option(
        False,
        "--local-registry",
        help="Pull images from the local Harbor registry (assumes registry is already deployed unless --install-local-registry is also set)",
    ),
    registry_url: Optional[str] = typer.Option(
        None,
        "--registry-url",
        help="Use an existing Harbor registry at host:port (skips deploy and mirroring, but rewrites image URLs)",
    ),
    mgmt_kubeconfig: Optional[str] = typer.Option(
        None,
        "--mgmt-kubeconfig",
        help="Kubeconfig for mgmt cluster (overrides cluster.yaml registry.mgmt_kubeconfig)",
    ),
    cluster_kubeconfig: Optional[str] = typer.Option(
        None,
        "--cluster-kubeconfig",
        help="Kubeconfig for the infra cluster — used with --install-local-registry to configure containerd trust on all nodes",
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
    # 0) Harbor registry (runs before any --install stages)
    # ------------------------------------------------------------------------------
    effective_registry_url: Optional[str] = None
    registry_project: str = "openstack"

    if registry_url:
        # Explicit URL override — no deploy or mirroring, just set the URL.
        effective_registry_url = registry_url
        registry_project = (cfg.registry.project if cfg.registry else None) or "openstack"
        typer.echo(f"[registry] Using existing registry at {effective_registry_url} (skipping deploy/mirror)")
    elif install_local_registry:
        # Deploy Harbor then use it for all image pulls.
        effective_registry_url = deploy_registry(
            cfg=cfg,
            workspace_root=WORKSPACE_ROOT,
            mgmt_kubeconfig=mgmt_kubeconfig,
            cluster_kubeconfig=cluster_kubeconfig,
        )
        registry_project = (cfg.registry.project if cfg.registry else None) or "openstack"
    elif local_registry:
        # Use an existing local registry — verify it is actually deployed first.
        effective_registry_url = _verify_and_get_registry_url(cfg, mgmt_kubeconfig)
        registry_project = (cfg.registry.project if cfg.registry else None) or "openstack"

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
                cfg=cfg,
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
            # Extract Keycloak admin password from config (secrets.yaml)
            kc_admin_pw = ""
            if cfg.keycloak and cfg.keycloak.monitoring:
                kc_admin_pw = cfg.keycloak.monitoring.password
            deploy_infrastructure(
                helm=helm,
                ssh=ssh,
                workspace_root=WORKSPACE_ROOT,
                infra_flag=infra,
                kubeconfig_path=kubeconfig_path,
                keycloak_admin_password=kc_admin_pw,
                registry_url=effective_registry_url,
                registry_project=registry_project,
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
                kubeconfig_path=kubeconfig_path,
                registry_url=effective_registry_url,
                registry_project=registry_project,
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
                managed_user=managed_user,
                ssh_key=ssh_key,
                ssh_password=ssh_password,
                registry_url=effective_registry_url,
                registry_project=registry_project,
            )

    finally:
        if client is not None:
            client.close()


# ------------------------------------------------------------------------------
# mgmt command — bootstrap a management cluster on a fresh Ubuntu node
# ------------------------------------------------------------------------------

@app.command()
def mgmt(
    config: str = typer.Argument(..., help="Cluster definition YAML (same format as deploy)"),
    ssh_host: Optional[str] = typer.Option(
        None,
        "--ssh-host",
        help="IP of the fresh Ubuntu node (overrides mgmt_cluster.host in config)",
    ),
    ssh_username: Optional[str] = typer.Option(
        None,
        "--ssh-username",
        help="SSH username on the target node (overrides mgmt_cluster.ssh_username)",
    ),
    ssh_password: Optional[str] = typer.Option(
        None,
        "--ssh-password",
        help="SSH password (overrides mgmt_cluster.ssh_password from secrets.yaml)",
    ),
    ssh_key: Optional[Path] = typer.Option(
        None,
        "--ssh-key",
        help="Path to SSH private key (alternative to password)",
    ),
    kubeconfig_out: Optional[str] = typer.Option(
        None,
        "--kubeconfig-out",
        help="Local path to save the generated kubeconfig (overrides mgmt_cluster.kubeconfig_output_path)",
    ),
    provisioning_interface: Optional[str] = typer.Option(
        None,
        "--provisioning-interface",
        help="Network interface used for bare-metal provisioning (overrides mgmt_cluster.provisioning_interface, default: ens18)",
    ),
    skip_harbor: bool = typer.Option(
        False,
        "--skip-harbor",
        help="Skip Harbor registry deployment even if install_harbor=true in config",
    ),
    debug: bool = typer.Option(False, "--debug"),
):
    """
    Bootstrap a management Kubernetes cluster on a fresh Ubuntu node,
    then install Metal3 / Ironic / CAPI on top of it.

    Example:

      daalu mgmt cluster-defs/cluster.yaml \\
        --ssh-host 192.168.0.163 \\
        --ssh-password admin10 \\
        --provisioning-interface ens18 \\
        --kubeconfig-out ~/.kube/daalu-mgmt-config
    """
    logger, run_id, log_path = init_logging(verbose=debug)

    typer.echo("")
    typer.secho("Daalu — Management Cluster Bootstrap", bold=True)
    typer.echo(f"  Run ID : {run_id}")
    typer.echo(f"  Logs   : {log_path}")
    typer.echo("")

    cfg: DaaluConfig = load_config(config)

    # Apply CLI overrides onto mgmt_cluster config
    if cfg.mgmt_cluster is None:
        # Build a minimal config from CLI flags
        if not ssh_host:
            typer.secho(
                "ERROR: --ssh-host is required when mgmt_cluster is not defined in cluster.yaml.",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(1)
        from daalu.bootstrap.mgmt.models import MgmtClusterConfig
        cfg = cfg.model_copy(
            update={"mgmt_cluster": MgmtClusterConfig(host=ssh_host)}
        )

    # Layer CLI flag overrides
    overrides: dict = {}
    if ssh_host:
        overrides["host"] = ssh_host
    if ssh_username:
        overrides["ssh_username"] = ssh_username
    if ssh_password:
        overrides["ssh_password"] = ssh_password
    if ssh_key:
        overrides["ssh_key"] = str(ssh_key)
    if kubeconfig_out:
        overrides["kubeconfig_output_path"] = kubeconfig_out
    if provisioning_interface:
        overrides["provisioning_interface"] = provisioning_interface
    if skip_harbor:
        overrides["install_harbor"] = False

    if overrides:
        cfg = cfg.model_copy(
            update={"mgmt_cluster": cfg.mgmt_cluster.model_copy(update=overrides)}
        )

    kubeconfig_path, harbor_url = MgmtClusterManager(cfg, WORKSPACE_ROOT).deploy()

    config_arg = Path(config).name

    typer.echo("")
    typer.secho("Management cluster is ready!", bold=True, fg=typer.colors.GREEN)
    typer.echo("")
    typer.echo(f"  Kubeconfig  : {kubeconfig_path}")
    if harbor_url:
        typer.echo(f"  Harbor UI   : https://{harbor_url}")
        typer.echo(f"  Harbor creds: admin / <registry.admin_password from secrets.yaml>")
    typer.echo("")
    typer.secho("Next steps:", bold=True)
    typer.echo("")
    typer.echo("  1. Verify the cluster:")
    typer.echo(f"       export KUBECONFIG={kubeconfig_path}")
    typer.echo("       kubectl get nodes")
    typer.echo("")
    typer.echo("  2. Deploy OpenStack components:")
    typer.echo(f"       daalu deploy {config_arg} \\")
    typer.echo( "         --managed-user builder \\")
    typer.echo( "         --managed-user-password <password> \\")
    typer.echo( "         --ssh-key ~/.ssh/openstack-key \\")
    typer.echo( "         --local-registry \\")
    typer.echo(f"         --mgmt-kubeconfig {kubeconfig_path}")
    typer.echo("")
    typer.echo("  3. To tear everything down:")
    typer.echo(f"       daalu clean {config_arg} --mgmt-kubeconfig {kubeconfig_path}")


@app.command("mirror-images")
def mirror_images(
    harbor_url: str = typer.Option(
        ...,
        "--harbor-url",
        help="Harbor host:port to mirror images into (e.g. 192.168.0.163:30003)",
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        help="Secrets YAML (defaults to cloud-config/secrets.yaml)",
    ),
) -> None:
    """
    Mirror all images from assets/ into Harbor.

    Reads images from assets/*/values.yaml (images.tags.*) and
    assets/*/extra_images.yaml, then uses skopeo to copy each one
    into Harbor. Already-mirrored images are skipped.
    """
    import yaml as _yaml
    from daalu.config.models import RegistryConfig
    from daalu.bootstrap.registry.manager import RegistryManager
    from daalu.logging.log import init_logging

    init_logging(verbose=True)
    secrets_path = Path(config) if config else WORKSPACE_ROOT / "cloud-config" / "secrets.yaml"
    try:
        raw = _yaml.safe_load(secrets_path.read_text()) or {}
        registry_cfg = RegistryConfig.model_validate(raw.get("registry", {}))
    except Exception as exc:
        typer.secho(f"[registry] Could not load config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    # Override harbor_hostname with the explicitly provided URL so mirroring
    # goes to the right place (dev VM may only reach the mgmt NIC, not the provisioning NIC).
    registry_cfg = registry_cfg.model_copy(update={"harbor_hostname": harbor_url})

    mgr = RegistryManager(
        registry_cfg=registry_cfg,
        workspace_root=WORKSPACE_ROOT,
        secrets_path=secrets_path,
    )
    # Point the access URL directly at the provided harbor_url
    mgr._harbor_access_url = harbor_url

    typer.echo(f"[registry] Mirroring images to https://{harbor_url} ...")
    mgr.mirror_images()
    typer.secho("\n[registry] Mirroring complete.", fg=typer.colors.GREEN)


@app.command("configure-registry-trust")
def configure_registry_trust(
    cluster_kubeconfig: str = typer.Option(
        ...,
        "--cluster-kubeconfig",
        help="Kubeconfig for the infra cluster whose nodes need to trust Harbor",
    ),
    mgmt_kubeconfig: Optional[str] = typer.Option(
        None,
        "--mgmt-kubeconfig",
        help="Kubeconfig for the mgmt cluster (overrides registry.mgmt_kubeconfig in config)",
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        help="Cluster/secrets YAML (defaults to cloud-config/secrets.yaml in workspace root)",
    ),
) -> None:
    """
    Configure containerd on every infra-cluster node to trust Harbor's
    self-signed certificate (writes skip_verify hosts.toml on each node).

    Run this after Harbor is deployed whenever:
      - You have new nodes that haven't been configured yet
      - You redeployed Harbor with a new IP / cert
      - Image pulls are failing with 'x509: certificate signed by unknown authority'
    """
    import yaml as _yaml
    from daalu.config.models import RegistryConfig
    from daalu.bootstrap.registry.manager import RegistryManager

    secrets_path = Path(config) if config else WORKSPACE_ROOT / "cloud-config" / "secrets.yaml"

    try:
        raw = _yaml.safe_load(secrets_path.read_text()) or {}
        registry_raw = raw.get("registry", {})
        registry_cfg = RegistryConfig.model_validate(registry_raw)
    except Exception as exc:
        typer.secho(
            f"[registry] Could not load registry config from {secrets_path}: {exc}",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)

    if mgmt_kubeconfig:
        registry_cfg = registry_cfg.model_copy(update={"mgmt_kubeconfig": mgmt_kubeconfig})

    try:
        mgr = RegistryManager(
            registry_cfg=registry_cfg,
            workspace_root=WORKSPACE_ROOT,
            secrets_path=WORKSPACE_ROOT / "cloud-config" / "secrets.yaml",
        )
        mgr.configure_cluster_registry_trust(cluster_kubeconfig)
        typer.secho(
            "\n[registry] All infra cluster nodes configured to trust Harbor.",
            fg=typer.colors.GREEN,
        )
    except Exception as exc:
        typer.secho(f"[registry] ERROR: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


# ------------------------------------------------------------------------------
# clean command — tear down the mgmt cluster and all workloads
# ------------------------------------------------------------------------------

@app.command()
def clean(
    config: str = typer.Argument(..., help="Cluster definition YAML"),
    mgmt_kubeconfig: Optional[str] = typer.Option(
        None,
        "--mgmt-kubeconfig",
        help="Path to mgmt cluster kubeconfig (defaults to mgmt_cluster.kubeconfig_output_path)",
    ),
    ssh_key: Optional[Path] = typer.Option(
        None,
        "--ssh-key",
        help="SSH private key for the mgmt node (overrides mgmt_cluster.ssh_key)",
    ),
    ssh_password: Optional[str] = typer.Option(
        None,
        "--ssh-password",
        help="SSH password for the mgmt node (overrides mgmt_cluster.ssh_password)",
    ),
    skip_workload_cluster: bool = typer.Option(
        False,
        "--skip-workload-cluster",
        help="Skip deleting the workload CAPI cluster (use if already gone)",
    ),
    no_wait: bool = typer.Option(
        False,
        "--no-wait",
        help="Do not wait for BareMetalHosts to deprovision before resetting the mgmt node",
    ),
    yes: bool = typer.Option(
        False,
        "--yes", "-y",
        help="Skip confirmation prompt",
    ),
    debug: bool = typer.Option(False, "--debug"),
):
    """
    Tear down everything daalu created:

      1. Delete the workload CAPI cluster so Metal3/Ironic can cleanly
         deprovision (wipe) bare-metal hosts.
      2. SSH to the mgmt node: kubeadm reset, flush CNI/iptables,
         wipe Harbor disk, remove Metal3/Ironic state.
      3. Remove local kubeconfigs and known_hosts entries.

    Example:

      daalu clean cluster-defs/cluster.yaml --mgmt-kubeconfig ~/.kube/daalu-mgmt-config

      # Skip waiting for deprovisioning (faster, but BMHs may not be wiped):
      daalu clean cluster-defs/cluster.yaml --no-wait

      # Already deleted the workload cluster manually:
      daalu clean cluster-defs/cluster.yaml --skip-workload-cluster
    """
    init_logging(verbose=debug)

    cfg: DaaluConfig = load_config(config)

    # Apply CLI SSH overrides
    if cfg.mgmt_cluster and (ssh_key or ssh_password):
        overrides: dict = {}
        if ssh_key:
            overrides["ssh_key"] = str(ssh_key)
        if ssh_password:
            overrides["ssh_password"] = ssh_password
        cfg = cfg.model_copy(
            update={"mgmt_cluster": cfg.mgmt_cluster.model_copy(update=overrides)}
        )

    mgmt_host = cfg.mgmt_cluster.host if cfg.mgmt_cluster else "unknown"
    harbor_disk = (
        cfg.registry.disk_device if cfg.registry and cfg.registry.disk_device else "none"
    )

    typer.echo("")
    typer.secho("Daalu — Teardown", bold=True)
    typer.echo("")
    typer.echo("  This will:")
    typer.echo("    1. Delete workload CAPI cluster (triggers bare-metal wipe via Ironic)")
    typer.echo(f"    2. SSH to {mgmt_host} → kubeadm reset, CNI flush, Harbor disk wipe ({harbor_disk})")
    typer.echo("    3. Remove local kubeconfigs and known_hosts entries")
    typer.echo("")

    if not yes:
        typer.confirm("Proceed with teardown?", abort=True)

    try:
        MgmtClusterCleaner(cfg).clean(
            mgmt_kubeconfig=mgmt_kubeconfig,
            skip_workload_cluster=skip_workload_cluster,
            wait_deprovision=not no_wait,
        )
    except Exception as exc:
        typer.secho(f"\n[clean] ERROR: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    typer.echo("")
    typer.secho("Teardown complete.", bold=True, fg=typer.colors.GREEN)
    typer.echo("")
    typer.echo("  To reinstall:")
    typer.echo(f"    daalu mgmt {config}")
    typer.echo("")


if __name__ == "__main__":
    app()
