# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/cli/app.py
from __future__ import annotations

import os
import shlex
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

from daalu.bootstrap.rgw.manager import RGWManager
from daalu.bootstrap.rgw.models import RGWConfig

from daalu.cli.helper import (
    inventory_path,
    read_hosts_from_inventory,
    read_group_from_inventory,
    plan_from_tags,
    maybe_read_kubeconfig_text,
)

from daalu.utils.execution import ExecutionContext
from daalu.utils.ssh_runner import SSHRunner
from daalu.utils.helpers import update_hosts_and_inventory

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

_ws_env = os.environ.get("WORKSPACE_ROOT")
WORKSPACE_ROOT = Path(_ws_env).resolve() if _ws_env else Path(__file__).resolve().parents[3]
os.environ["WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)


# ------------------------------------------------------------------------------
# Install targets
# ------------------------------------------------------------------------------

ALL_TARGETS: Set[str] = {
    "cluster-api",
    "nodes",
    "ceph",
    "csi",
    "rgw",
    "infrastructure",
    "monitoring",
    "openstack",
}

# Targets that are implied by another target. When the LHS is in the install
# plan, every target on the RHS is added too. This keeps `--install csi` doing
# what users intuitively expect: block storage AND object storage.
IMPLIED_TARGETS: dict[str, Set[str]] = {
    "csi": {"rgw"},
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

    # Expand implied targets (e.g. csi → also rgw)
    for target, implied in IMPLIED_TARGETS.items():
        if target in items:
            items |= implied

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


def deploy_cluster_api_tinkerbell(
    *,
    cfg,
    workspace_root: Path,
    mgmt_context: Optional[str],
) -> bool:
    """
    Deploy a CAPT-backed workload cluster:
      1. Apply manifests from assets/tinkerbell/cluster-api/ (substituting ${ VAR } placeholders)
      2. Phase 1 — provision control-plane node(s) via Rufio + wait for workflow success
      3. Wait for KCP to initialize (kubeadm init)
      4. Fetch workload kubeconfig + install Cilium CNI
      5. Phase 2 — provision worker node(s)
      6. Wait for cluster to be fully ready

    Returns True if cluster became ready, False on timeout.
    """
    import re
    import subprocess as _sp
    from daalu.bootstrap.mgmt.capt_provisioner import (
        provision_phase,
        wait_for_kcp_initialized,
        wait_for_kcp_ready,
        fetch_workload_kubeconfig,
        install_cilium,
        wait_for_workload_api_server_stable,
        wait_for_cluster_ready,
    )

    ca = cfg.cluster_api
    if ca is None:
        raise RuntimeError("cluster_api config block is required for Tinkerbell CAPI deploy")

    substitutions = {
        "CLUSTER_NAME": ca.cluster_name,
        "NAMESPACE": ca.namespace,
        "SERVICE_CIDR": ca.service_cidr,
        "POD_CIDR": ca.pod_cidr,
        "CLUSTER_APIENDPOINT_HOST": ca.control_plane_vip,
        "IMAGE_URL": ca.image_url,
        "KUBERNETES_VERSION": ca.kubernetes_version,
        "CONTROL_PLANE_MACHINE_COUNT": str(ca.control_plane_replicas),
        "WORKER_MACHINE_COUNT": str(ca.worker_replicas),
        "IMAGE_USERNAME": ca.image_username,
        "SSH_PUB_KEY_CONTENT": ca.ssh_public_key,
        "HEGEL_URL": f"{getattr(ca, 'ironic_http_base', 'http://10.10.0.9').rstrip('/')}:50061",
        "PROVISIONING_GATEWAY": getattr(ca, 'provisioning_gateway', '10.10.0.100'),
    }

    def _substitute(text: str) -> str:
        def _replace(m: re.Match) -> str:
            key = m.group(1).strip()
            if key not in substitutions:
                raise KeyError(
                    f"No substitution value for '${{ {key} }}' in Tinkerbell CAPI manifest"
                )
            return substitutions[key]
        return re.sub(r'\$\{\s+(\w+)\s+\}', _replace, text)

    manifests_dir = workspace_root / "assets" / "tinkerbell" / "cluster-api"
    kubeconfig = (
        str(Path(cfg.mgmt_cluster.kubeconfig_output_path).expanduser())
        if cfg.mgmt_cluster
        else None
    )

    kc_args = ["--kubeconfig", kubeconfig] if kubeconfig else []
    ctx_args = ["--context", mgmt_context] if mgmt_context else []

    ns = ca.namespace
    if ns and ns != "default":
        _sp.run(
            ["kubectl", *kc_args, *ctx_args, "apply", "-f", "-"],
            input=f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {ns}\n",
            text=True, check=True,
        )

    for manifest in sorted(manifests_dir.glob("*.yaml")):
        typer.echo(f"  [tinkerbell] Applying {manifest.name}...")
        _sp.run(
            ["kubectl", *kc_args, *ctx_args, "apply", "-f", "-"],
            input=_substitute(manifest.read_text()),
            text=True, check=True,
        )
    typer.secho("  [tinkerbell] Cluster API manifests applied.", fg=typer.colors.GREEN)

    # CAPT names worker workflows after the MachineDeployment, which always contains
    # "-workers-" (e.g. auto-openstack-infra-workers-ldl9c-pw5jf).  Control-plane
    # workflows do not contain this token.  Use these filters to scope each phase
    # correctly on both first runs and idempotent re-runs.
    worker_wf_token = f"{ca.cluster_name}-workers-"

    typer.echo("\n  [tinkerbell] Phase 1 — provisioning control plane node(s)...")
    cp_workflows = provision_phase(
        phase_name="control-plane",
        namespace=ca.namespace,
        kc_args=kc_args,
        ctx_args=ctx_args,
        workflow_name_excludes=worker_wf_token,
    )

    typer.echo(
        "\n  [tinkerbell] Waiting for KubeadmControlPlane to initialize "
        "(kubeadm init running — ~10 min)..."
    )
    wait_for_kcp_initialized(
        cluster_name=ca.cluster_name,
        namespace=ca.namespace,
        kc_args=kc_args,
        ctx_args=ctx_args,
        timeout=1800,
    )

    # Fetch workload kubeconfig — the secret exists right after KCP init.
    wl_kubeconfig = fetch_workload_kubeconfig(
        cluster_name=ca.cluster_name,
        namespace=ca.namespace,
        kc_args=kc_args,
        ctx_args=ctx_args,
    )

    # Wait for the workload API server to be consistently reachable before installing
    # Cilium.  The CAPI kubeconfig secret is created during early kubeadm init while
    # the API server may still restart (kubeadm progresses through phases) or the node
    # undergoes a second OS-level reboot (cloud-init/systemd finalising network).
    # Installing Cilium during that window fails because the API server is unreachable.
    if wl_kubeconfig:
        typer.echo(
            "\n  [tinkerbell] Waiting for workload API server to stabilise "
            "before installing Cilium..."
        )
        wait_for_workload_api_server_stable(
            workload_kubeconfig=wl_kubeconfig,
            consecutive_ok=3,
            poll_interval=10,
            timeout=600,
        )

    # Install CNI before phase 2: CAPT only schedules worker Machines after KCP has
    # readyReplicas >= 1, which requires the control-plane node to be Ready (CNI up).
    # Installing Cilium here unblocks CAPT so worker Workflows appear promptly.
    if not ca.cilium_version:
        typer.secho(
            "  [tinkerbell] ERROR: cilium_version not set — cannot install CNI. "
            "Nodes will never become Ready. Set cilium_version in cluster_api config.",
            fg=typer.colors.RED,
        )
        return False
    if not wl_kubeconfig:
        typer.secho(
            "  [tinkerbell] ERROR: workload kubeconfig unavailable — cannot install Cilium.",
            fg=typer.colors.RED,
        )
        return False
    cni_ok = install_cilium(version=ca.cilium_version, kubeconfig=wl_kubeconfig)
    if not cni_ok:
        typer.secho(
            "  [tinkerbell] ERROR: Cilium install failed — nodes will not become Ready. "
            "Fix Cilium then re-run.",
            fg=typer.colors.RED,
        )
        return False

    # Wait for the control-plane node to be Ready in the workload cluster — this is the
    # real signal that CAPT will start scheduling worker Machines.  We use the workload
    # kubeconfig directly rather than KCP readyReplicas on the mgmt cluster because
    # readyReplicas is gated on providerID reconciliation (which requires a manual patch
    # in our setup) and would block indefinitely.
    if wl_kubeconfig:
        typer.echo(
            "\n  [tinkerbell] Waiting for control-plane node Ready in workload cluster..."
        )
        wait_for_cluster_ready(
            workload_kubeconfig=wl_kubeconfig,
            expected_nodes=ca.control_plane_replicas,
            timeout=600,
        )

    typer.echo("\n  [tinkerbell] Phase 2 — provisioning worker node(s)...")
    provision_phase(
        phase_name="workers",
        namespace=ca.namespace,
        kc_args=kc_args,
        ctx_args=ctx_args,
        workflow_name_contains=worker_wf_token,
        workflow_appear_timeout=300,
    )

    typer.echo("\n  [tinkerbell] Waiting for cluster to be fully ready...")
    expected_nodes = ca.control_plane_replicas + ca.worker_replicas
    ready = wait_for_cluster_ready(
        workload_kubeconfig=wl_kubeconfig or f"/tmp/kubeconfig-{ca.cluster_name}.yaml",
        expected_nodes=expected_nodes,
        timeout=1800,
    )
    if not ready:
        typer.secho(
            "  [tinkerbell] WARNING: timed out waiting for cluster — check node status manually.",
            fg=typer.colors.YELLOW,
        )
    return ready


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

    # Regenerate inventory from the live workload cluster so we always use
    # current node IPs rather than stale entries from a previous deployment.
    wl_kc = Path(f"/tmp/kubeconfig-{cfg.cluster_api.cluster_name}.yaml")
    if wl_kc.is_file():
        typer.echo("[nodes] Refreshing inventory from live cluster nodes...")
        update_hosts_and_inventory(
            kubeconfig=wl_kc,
            workspace_root=workspace_root,
            domain_suffix=domain_suffix,
            ctx=type("_Ctx", (), {"logger": None, "dry_run": False})(),
        )

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


def _push_authorized_key(
    address: str,
    port: int,
    username: str,
    password: str,
    pub_key_content: str,
) -> None:
    """
    Connect to *address* via password auth and append *pub_key_content* to
    ~/.ssh/authorized_keys if not already present.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=address,
        port=port,
        username=username,
        password=password,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        script = (
            "set -e\n"
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
            f"KEY={shlex.quote(pub_key_content.strip())}\n"
            "grep -qxF \"$KEY\" ~/.ssh/authorized_keys 2>/dev/null "
            "|| echo \"$KEY\" >> ~/.ssh/authorized_keys\n"
            "chmod 600 ~/.ssh/authorized_keys\n"
        )
        stdin, stdout, stderr = client.exec_command("bash -s")
        stdin.write(script)
        stdin.channel.shutdown_write()
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            raise RuntimeError(
                f"SSH key install on {address} failed: {stderr.read().decode().strip()}"
            )
    finally:
        client.close()


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

    # For additional hosts that use password auth (no pkey_path) and we have
    # an SSH key: install the public key via password, then switch to key auth
    # so all Ceph hosts use the same credentials going forward.
    if ssh_key:
        pub_key_path = Path(f"{ssh_key}.pub")
        if pub_key_path.exists():
            pub_key_content = pub_key_path.read_text().strip()
            for host in ceph_hosts:
                if host.password and not host.pkey_path:
                    typer.echo(
                        f"[ceph] Installing SSH key on {host.hostname} ({host.address})..."
                    )
                    _push_authorized_key(
                        address=host.address,
                        port=host.port,
                        username=host.username,
                        password=host.password,
                        pub_key_content=pub_key_content,
                    )
                    host.pkey_path = str(ssh_key)
                    typer.echo(f"[ceph] SSH key installed on {host.hostname}")
        else:
            typer.echo(
                f"[ceph] Warning: {pub_key_path} not found — skipping SSH key install on password-auth hosts"
            )

    _pool_size = cfg.ceph.pool_replication_size if cfg and cfg.ceph else 3
    _pool_min_size = cfg.ceph.pool_min_size if cfg and cfg.ceph else 2
    _public_network = cfg.ceph.public_network if cfg and cfg.ceph else None
    CephManager(
        bus=EventBus(observers=[ConsoleObserver()])
    ).deploy(
        ceph_hosts,
        CephConfig(
            version=ceph_version,
            image=ceph_image,
            apply_osds_all_devices=True,
            pool_replication_size=_pool_size,
            pool_min_size=_pool_min_size,
            public_network=_public_network,
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


def deploy_rgw(
    *,
    ceph_hosts: List[CephHost],
    cfg=None,
) -> None:
    """
    Deploy a Ceph RGW (S3-compatible object storage gateway).

    Reads optional configuration from cfg.ceph.rgw if present; otherwise uses
    sensible defaults (port 7480, single placement, no default user).
    """
    typer.echo("\n[rgw] Installing Ceph RGW (S3 object storage)...")

    rgw_cfg = RGWConfig()
    if cfg is not None and getattr(cfg, "ceph", None) is not None:
        ceph_section = cfg.ceph
        if getattr(ceph_section, "rgw", None) is not None:
            r = ceph_section.rgw
            rgw_cfg = RGWConfig(
                realm=getattr(r, "realm", rgw_cfg.realm),
                placement_count=getattr(r, "placement_count", rgw_cfg.placement_count),
                port=getattr(r, "port", rgw_cfg.port),
                ready_timeout_secs=getattr(r, "ready_timeout_secs", rgw_cfg.ready_timeout_secs),
                default_user_id=getattr(r, "default_user_id", rgw_cfg.default_user_id),
                default_user_display_name=getattr(
                    r, "default_user_display_name", rgw_cfg.default_user_display_name
                ),
            )

    RGWManager(
        bus=EventBus(observers=[ConsoleObserver()]),
        ceph_hosts=ceph_hosts,
    ).deploy(rgw_cfg)


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
    if effective_kubeconfig:
        effective_kubeconfig = str(Path(effective_kubeconfig).expanduser())

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
    ssh_key: Optional[Path] = None,
    ssh_username: Optional[str] = None,
    ssh_password: Optional[str] = None,
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

    # Resolve SSH creds: explicit args > mgmt_cluster config > model defaults
    _mc = getattr(cfg, "mgmt_cluster", None)
    effective_ssh_key = ssh_key or (getattr(_mc, "ssh_key", None) if _mc else None)
    effective_ssh_username = ssh_username or (getattr(_mc, "ssh_username", None) if _mc else None) or "ubuntu"
    effective_ssh_password = ssh_password or (getattr(_mc, "ssh_password", None) if _mc else None)

    mgr.deploy_harbor(
        mgmt_kubeconfig=effective_kubeconfig,
        ssh_key=str(Path(effective_ssh_key).expanduser()) if effective_ssh_key else None,
        ssh_username=effective_ssh_username,
        ssh_password=effective_ssh_password,
        cluster_kubeconfig=cluster_kubeconfig,
    )
    mgr.mirror_images()

    url = mgr.harbor_registry_url()
    typer.echo(f"\n[registry] Harbor available at https://{url}")
    return url


def teardown_infrastructure(
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
    Tear down infrastructure components installed by deploy_infrastructure.
    Components are uninstalled in reverse deployment order.
    """
    typer.echo("\n[infrastructure] Tearing down infrastructure components...")

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
    ).teardown(components)


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
        help="Components to install: cluster-api,nodes,ceph,csi,infrastructure,monitoring,openstack or all",
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
    ssh_username: Optional[str] = typer.Option(None, "--ssh-username", help="Initial SSH user on nodes (default: from config or 'ubuntu')"),
    ssh_password: Optional[str] = typer.Option(None, "--ssh-password", help="SSH password (default: from mgmt_cluster.ssh_password in secrets.yaml)"),
    ssh_key: Optional[Path] = typer.Option(None, "--ssh-key", help="SSH private key path (default: from mgmt_cluster.ssh_key in config)"),
    managed_user: Optional[str] = typer.Option(None, "--managed-user", help="Linux user to create on nodes (default: from mgmt_cluster.managed_user in config)"),
    managed_user_password: Optional[str] = typer.Option(None, "--managed-user-password", help="Password for managed user (default: from mgmt_cluster.managed_user_password in secrets.yaml)"),
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

    # Resolve SSH / managed-user values: CLI flag > config > built-in default
    _mc = cfg.mgmt_cluster
    ssh_username = ssh_username or (getattr(_mc, "ssh_username", None) if _mc else None) or "ubuntu"
    ssh_password = ssh_password or (getattr(_mc, "ssh_password", None) if _mc else None)
    ssh_key = ssh_key or (Path(_mc.ssh_key).expanduser() if _mc and _mc.ssh_key else None)
    managed_user = managed_user or (getattr(_mc, "managed_user", None) if _mc else None) or "builder"
    managed_user_password = managed_user_password or (getattr(_mc, "managed_user_password", None) if _mc else None)
    if not managed_user_password:
        raise typer.BadParameter(
            "managed_user_password is required. Set it via --managed-user-password or "
            "mgmt_cluster.managed_user_password in cloud-config/secrets.yaml."
        )

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
            ssh_key=ssh_key,
            ssh_username=ssh_username,
            ssh_password=ssh_password,
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

    _capi_ready: Optional[bool] = None

    if "cluster-api" in install_plan:
        typer.echo("\n[cluster-api] Installing Cluster API...")

        # Use CLI --ssh-key to derive ssh_public_key_path if not set in config
        if ssh_key and cfg.cluster_api and not str(cfg.cluster_api.ssh_public_key_path).strip("."):
            pub_key_path = Path(f"{ssh_key}.pub")
            if pub_key_path.expanduser().is_file():
                cfg.cluster_api.ssh_public_key_path = pub_key_path

        provider = getattr(cfg.cluster_api, "provider", "metal3")

        if provider == "metal3":
            deploy_cluster_api_metal3(
                cfg=cfg,
                workspace_root=WORKSPACE_ROOT,
                mgmt_context=mgmt_context,
                dry_run=dry_run,
            )
        elif provider == "tinkerbell":
            _capi_ready = deploy_cluster_api_tinkerbell(
                cfg=cfg,
                workspace_root=WORKSPACE_ROOT,
                mgmt_context=mgmt_context,
            )
        else:
            deploy_cluster_api_generic(
                cfg=cfg,
                workspace_root=WORKSPACE_ROOT,
                mgmt_context=mgmt_context,
            )
    else:
        _capi_ready = None  # cluster-api step not in plan

    # ------------------------------------------------------------------------------
    # 2) Node bootstrap
    # ------------------------------------------------------------------------------
    # Derive expected workload kubeconfig path
    _workload_kc = (
        f"/tmp/kubeconfig-{cfg.cluster_api.cluster_name}.yaml"
        if cfg.cluster_api
        else None
    )
    _workload_kc_exists = _workload_kc and os.path.isfile(_workload_kc)

    if "nodes" in install_plan:
        # Use image_username from cluster_api config if --ssh-username was not
        # explicitly provided (i.e. still the default "ubuntu").  Metal3 nodes
        # are provisioned with image_username, so we must SSH as that user.
        # image_username is baked into the provisioned OS image — always prefer it
        # over the generic ssh_username (which comes from mgmt_cluster, not infra nodes).
        # Only skip if --ssh-username was the sentinel value meaning "not set by user"
        # is impossible to detect here, so we just always use image_username when present.
        image_username = cfg.cluster_api and getattr(cfg.cluster_api, "image_username", None)
        effective_ssh_user = image_username or ssh_username
        if image_username and image_username != ssh_username:
            typer.echo(f"[nodes] Using image_username '{effective_ssh_user}' from cluster config for SSH")

        # Derive private key from cluster_api.ssh_public_key_path if not explicitly set.
        # Nodes are provisioned with that public key, so the matching private key is needed.
        effective_node_ssh_key = ssh_key
        if effective_node_ssh_key is None and cfg.cluster_api:
            pub = getattr(cfg.cluster_api, "ssh_public_key_path", None)
            if pub:
                priv = Path(str(pub)).expanduser()
                if str(priv).endswith(".pub"):
                    priv = Path(str(priv)[:-4])
                if priv.is_file():
                    effective_node_ssh_key = priv
                    typer.echo(f"[nodes] Using SSH key derived from cluster_api.ssh_public_key_path: {priv}")

        # Skip nodes if the workload cluster isn't reachable yet.
        # This happens when cluster-api timed out or was not in the install plan.
        if not _workload_kc_exists:
            typer.secho(
                f"[nodes] Skipping node bootstrap — workload kubeconfig not found at "
                f"{_workload_kc}.\n"
                f"  Run 'daalu deploy --install nodes' once the cluster is ready.",
                fg=typer.colors.YELLOW,
            )
        else:
            deploy_nodes(
                cfg=cfg,
                workspace_root=WORKSPACE_ROOT,
                cluster_name=cluster_name,
                node_tags=node_tags,
                ssh_username=effective_ssh_user,
                ssh_key=effective_node_ssh_key,
                domain_suffix=domain_suffix,
                managed_user=managed_user,
                managed_user_password=managed_user_password,
            )

            # After nodes are bootstrapped, configure containerd trust for the local
            # registry so subsequent image pulls (ceph, csi, openstack, etc.) succeed.
            if effective_registry_url:
                _infra_kubeconfig = f"/tmp/kubeconfig-{cfg.cluster_api.cluster_name}.yaml"
                if os.path.isfile(_infra_kubeconfig):
                    typer.echo("\n[registry] Configuring infra cluster nodes to trust local Harbor registry...")
                    try:
                        from daalu.bootstrap.registry.manager import RegistryManager
                        from daalu.config.models import RegistryConfig
                        _reg_cfg = cfg.registry or RegistryConfig()
                        _reg_mgr = RegistryManager(
                            registry_cfg=_reg_cfg,
                            workspace_root=WORKSPACE_ROOT,
                            secrets_path=WORKSPACE_ROOT / "cloud-config" / "secrets.yaml",
                        )
                        _reg_mgr.configure_cluster_registry_trust(_infra_kubeconfig)
                        typer.echo("[registry] Registry trust configured on all infra nodes.")
                    except Exception as _trust_exc:
                        logger.warning("[registry] Registry trust configuration failed (non-fatal): %s", _trust_exc)
                else:
                    logger.debug("[registry] Infra kubeconfig not found at %s — skipping trust config", _infra_kubeconfig)

    # ------------------------------------------------------------------------------
    # Shared controller SSH (for Ceph/CSI/Infra/OpenStack)
    # Only open if the install plan includes components that need it.
    # Skip entirely for registry-only or cluster-api/nodes-only runs.
    # ------------------------------------------------------------------------------
    _needs_controller_ssh = install_plan & {"ceph", "csi", "infrastructure", "monitoring", "openstack"}

    client: Optional[paramiko.SSHClient] = None
    helm = None
    ssh = None
    kubeconfig_path = None
    ceph_hosts: List[CephHost] = []

    # If the workload cluster is not yet ready, skip all steps that require SSH
    # into the workload cluster nodes (ceph, csi, infrastructure, openstack, monitoring).
    if _needs_controller_ssh and not _workload_kc_exists:
        typer.secho(
            "\n[deploy] Skipping infrastructure/openstack/ceph steps — workload cluster "
            f"kubeconfig not found at {_workload_kc}.\n"
            "  Run 'daalu deploy --install nodes,ceph,csi,infrastructure,monitoring,openstack' "
            "once the cluster is ready.",
            fg=typer.colors.YELLOW,
        )
        return

    try:
        if _needs_controller_ssh:
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

        # ---------------------------------------------------------------------------
        # 4b) RGW (Ceph S3 object gateway)
        # ---------------------------------------------------------------------------
        if "rgw" in install_plan:
            deploy_rgw(
                ceph_hosts=ceph_hosts,
                cfg=cfg,
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
    skip_provisioning_stack: bool = typer.Option(
        False,
        "--skip-provisioning-stack",
        help="Install Kubernetes + Cilium only; skip cert-manager, CAPI, and the provider stack (Tinkerbell/Metal3). Use this to manually walk through provisioning stack installation afterwards.",
    ),
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        help="Bare-metal provisioning backend to install: tinkerbell (default), metal3, proxmox",
    ),
    debug: bool = typer.Option(False, "--debug"),
):
    """
    Bootstrap a management Kubernetes cluster on a fresh Ubuntu node, then
    install a bare-metal provisioning stack on top.

    The --provider flag controls which provisioning backend is installed:

      tinkerbell  (default) — Tinkerbell stack + CAPT
      metal3                — Ironic + Baremetal Operator + CAPM3
      proxmox               — Cluster API Provider for Proxmox (CAPO)

    The provider can also be set via mgmt_cluster.provider in cluster.yaml.
    The CLI flag takes precedence over the config file.

    Example:

      daalu mgmt cluster-defs/cluster.yaml \\
        --provider tinkerbell \\
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

    # ------------------------------------------------------------------
    # Resolve bare-metal provider
    # Priority: --provider flag > mgmt_cluster.provider in config > default (tinkerbell)
    # ------------------------------------------------------------------
    from daalu.bootstrap.mgmt.models import BaremetalProvider

    _valid_providers = {p.value for p in BaremetalProvider}

    if provider is not None:
        if provider not in _valid_providers:
            typer.secho(
                f"ERROR: --provider '{provider}' is not valid. "
                f"Choose one of: {', '.join(sorted(_valid_providers))}",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(1)
        resolved_provider = BaremetalProvider(provider)
    else:
        # Falls back to whatever is in mgmt_cluster.provider (already defaults to tinkerbell)
        resolved_provider = cfg.mgmt_cluster.provider

    typer.echo(f"  Provider: {resolved_provider.value}")
    typer.echo("")

    # Layer CLI flag overrides
    overrides: dict = {"provider": resolved_provider}
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
    if skip_provisioning_stack:
        overrides["skip_provisioning_stack"] = True

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
        import logging as _logging
        _logging.getLogger("daalu").exception("[registry] Could not load config: %s: %s", type(exc).__name__, exc)
        typer.secho(f"[registry] Could not load config: {type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
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
        import logging as _logging
        _logging.getLogger("daalu").exception("[registry] Could not load registry config from %s: %s: %s", secrets_path, type(exc).__name__, exc)
        typer.secho(
            f"[registry] Could not load registry config from {secrets_path}: {type(exc).__name__}: {exc}",
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
        import logging as _logging
        _logging.getLogger("daalu").exception("[registry] ERROR: %s: %s", type(exc).__name__, exc)
        typer.secho(f"[registry] ERROR: {type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


# ------------------------------------------------------------------------------
# clean command — tear down the mgmt cluster and all workloads
# ------------------------------------------------------------------------------

@app.command()
def clean(
    config: str = typer.Argument(..., help="Cluster definition YAML"),
    install: Optional[str] = typer.Option(
        None,
        "--install",
        help="Tear down only specific components: infrastructure,openstack,monitoring or all. "
             "When set, runs component-level teardown instead of CAPI cluster deletion.",
    ),
    infra: Optional[str] = typer.Option(
        None,
        "--infra",
        help="Infrastructure sub-components to clean (e.g. metallb,argocd or all). "
             "Only used when --install infrastructure is set.",
    ),
    kubeconfig: Optional[str] = typer.Option(
        None,
        "--kubeconfig",
        help="Kubeconfig for the target cluster (required with --install).",
    ),
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
    wipe_mgmt: bool = typer.Option(
        False,
        "--wipe-mgmt",
        help="Also destroy the management cluster (kubeadm reset, wipe Harbor disk, remove Metal3 state). "
             "Without this flag only the workload CAPI cluster is deleted.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes", "-y",
        help="Skip confirmation prompt",
    ),
    debug: bool = typer.Option(False, "--debug"),
):
    """
    Tear down the workload CAPI cluster, or specific installed components.

    Without --install: deletes the workload CAPI cluster (triggers bare-metal wipe via Ironic).
    With --install: runs helm uninstall + cleanup for the specified component group.

      # Tear down all infrastructure components (metallb, argocd, keycloak, etc.):
      daalu clean cluster-defs/cluster.yaml --install infrastructure --kubeconfig ~/.kube/config

      # Tear down only specific infra sub-components:
      daalu clean cluster-defs/cluster.yaml --install infrastructure --infra metallb,argocd --kubeconfig ~/.kube/config

      # Full CAPI cluster teardown (default behaviour):
      daalu clean cluster-defs/cluster.yaml --mgmt-kubeconfig ~/.kube/daalu-mgmt-config

      # Also destroy mgmt k8s + Harbor:
      daalu clean cluster-defs/cluster.yaml --mgmt-kubeconfig ~/.kube/daalu-mgmt-config --wipe-mgmt
    """
    init_logging(verbose=debug)

    cfg: DaaluConfig = load_config(config)

    # ------------------------------------------------------------------
    # Component-level teardown (--install infrastructure, etc.)
    # ------------------------------------------------------------------
    if install:
        install_targets = resolve_install_plan(install)

        if not kubeconfig:
            typer.secho(
                "ERROR: --kubeconfig is required when using --install",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(1)

        _mc = cfg.mgmt_cluster
        _ssh_key = ssh_key or (Path(_mc.ssh_key).expanduser() if _mc and _mc.ssh_key else None)
        _ssh_password = ssh_password or (getattr(_mc, "ssh_password", None) if _mc else None)
        _host = _mc.host if _mc else None

        if not _host:
            typer.secho(
                "ERROR: mgmt_cluster.host is required for SSH-based teardown",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(1)

        typer.echo("")
        typer.secho("Daalu — Component Teardown", bold=True)
        typer.echo(f"  Components : {install}")
        if infra:
            typer.echo(f"  Infra      : {infra}")
        typer.echo(f"  Kubeconfig : {kubeconfig}")
        typer.echo("")

        if not yes:
            typer.confirm("Proceed with component teardown?", abort=True)

        try:
            from daalu.helm.cli_runner import HelmCliRunner
            from daalu.utils.ssh_runner import SSHRunner

            _paramiko_client = paramiko.SSHClient()
            _paramiko_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            _paramiko_client.connect(
                hostname=_host,
                username=_mc.ssh_username if _mc else "ubuntu",
                key_filename=str(_ssh_key) if _ssh_key else None,
                password=_ssh_password,
            )
            ssh = SSHRunner(_paramiko_client)

            with _paramiko_client:
                helm = HelmCliRunner(ssh=ssh)

                keycloak_admin_password = (
                    cfg.keycloak.monitoring.password
                    if cfg.keycloak and cfg.keycloak.monitoring and cfg.keycloak.monitoring.password
                    else ""
                )

                if "infrastructure" in install_targets or "all" in install_targets:
                    teardown_infrastructure(
                        helm=helm,
                        ssh=ssh,
                        workspace_root=WORKSPACE_ROOT,
                        infra_flag=infra,
                        kubeconfig_path=kubeconfig,
                        keycloak_admin_password=keycloak_admin_password,
                    )

        except Exception as exc:
            import logging as _logging
            _logging.getLogger("daalu").exception("[clean] ERROR: %s: %s", type(exc).__name__, exc)
            typer.secho(f"\n[clean] ERROR: {type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

        typer.echo("")
        typer.secho("Component teardown complete.", bold=True, fg=typer.colors.GREEN)
        return

    # ------------------------------------------------------------------
    # Full CAPI cluster teardown (default path)
    # ------------------------------------------------------------------

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
    if wipe_mgmt:
        typer.echo(f"    2. SSH to {mgmt_host} → kubeadm reset, CNI flush, Harbor disk wipe ({harbor_disk})")
        typer.echo("    3. Remove local kubeconfigs and known_hosts entries")
    else:
        typer.echo("    2. Remove workload kubeconfig from /tmp")
        typer.secho("    (mgmt cluster kept running — add --wipe-mgmt to also destroy it)", dim=True)
    typer.echo("")

    if not yes:
        typer.confirm("Proceed with teardown?", abort=True)

    try:
        MgmtClusterCleaner(cfg).clean(
            mgmt_kubeconfig=mgmt_kubeconfig,
            skip_workload_cluster=skip_workload_cluster,
            wait_deprovision=not no_wait,
            wipe_mgmt=wipe_mgmt,
        )
    except Exception as exc:
        import logging as _logging
        _logging.getLogger("daalu").exception("[clean] ERROR: %s: %s", type(exc).__name__, exc)
        typer.secho(f"\n[clean] ERROR: {type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    typer.echo("")
    typer.secho("Teardown complete.", bold=True, fg=typer.colors.GREEN)
    typer.echo("")
    if wipe_mgmt:
        typer.echo("  To reinstall from scratch:")
        typer.echo(f"    daalu mgmt {config}")
    else:
        typer.echo("  Management cluster is still running. To redeploy the workload cluster:")
        typer.echo(f"    daalu deploy {config} --mgmt-kubeconfig ~/.kube/daalu-mgmt-config --install cluster-api,nodes,ceph,csi,infrastructure,monitoring,openstack --local-registry --registry-url 10.10.0.9:30003")
    typer.echo("")


if __name__ == "__main__":
    app()
