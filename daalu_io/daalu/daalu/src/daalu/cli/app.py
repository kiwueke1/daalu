# src/daalu/cli/app.py
import os
from pathlib import Path
from typing import Optional, List

import typer
import yaml


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


app = typer.Typer(help="Daalu Deployment CLI")
app.add_typer(hpc_cli, name="hpc")


# Resolve workspace root (used by bootstrap managers to find playbooks/)
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("WORKSPACE_ROOT", str(WORKSPACE_ROOT))


@app.command()
def deploy(
    config: str = typer.Argument(..., help="Workload cluster config (for Helm stage)"),
    context: Optional[str] = typer.Option(
        None, "--context", "-c", help="Workload cluster kube-context"
    ),
    mgmt_context: Optional[str] = typer.Option(
        None, "--mgmt-context", help="Management cluster kube-context (for Cluster API + setup phases)"
    ),
    cluster_name: str = typer.Option(
        "openstack-infra", "--cluster-name", help="Cluster API Cluster name"
    ),
    cluster_namespace: str = typer.Option(
        "default", "--cluster-namespace", help="Cluster API Cluster namespace"
    ),
    skip_clusterapi: bool = typer.Option(
        False, "--skip-clusterapi", help="Skip Cluster API bootstrap"
    ),
    skip_setup: bool = typer.Option(
        False, "--skip-setup", help="Skip setup phase (kubeconfig/Cilium/hosts/inventory)"
    ),
    skip_nodes: bool = typer.Option(
        False, "--skip-nodes", help="Skip node OS bootstrap (apparmor/netplan/ssh/inotify/istio)"
    ),
    node_tags: Optional[str] = typer.Option(
        None,
        "--node-tags",
        help="Comma-separated subset of roles to run on nodes (apparmor,netplan,ssh,inotify,istio). Default: all.",
    ),
    # SSH options for node bootstrap
    ssh_username: str = typer.Option(
        "ubuntu", "--ssh-username", help="SSH username for nodes"
    ),
    ssh_password: Optional[str] = typer.Option(
        None, "--ssh-password", help="SSH password (if not using key)"
    ),
    ssh_key: Optional[Path] = typer.Option(
        None, "--ssh-key", help="Path to SSH private key"
    ),
    managed_user: str = typer.Option(
        "kez", "--managed-user", help="User to create/configure on nodes"
    ),
    managed_user_password: str = typer.Option(
        "admin10", "--managed-user-password", help="Password for managed user"
    ),
    domain_suffix: str = typer.Option(
        "net.daalu.io", "--domain-suffix", help="Domain suffix for /etc/hosts FQDNs"
    ),
    skip_ceph: bool = typer.Option(
        False, "--skip-ceph", help="Skip Ceph bootstrap"
    ),
    debug: bool = typer.Option(
        False, "--debug", "-d", help="Enable Helm debug output (passes --debug to helm)"
    ),
    ceph_version: str = typer.Option(
        "18.2.1", "--ceph-version", help="Ceph version (forms quay.io/ceph/ceph:v<version> if --ceph-image not set)"
    ),
    ceph_image: Optional[str] = typer.Option(
        None, "--ceph-image", help="Explicit cephadm image (overrides --ceph-version)"
    ),
):
    """
    Full deployment workflow:
      1) Bootstrap on MANAGEMENT cluster (Cluster API)
      2) Setup phase on management + workload clusters (kubeconfig, Cilium, hosts/inventory, labels/taints)
      3) Node OS bootstrap via SSH on servers (apparmor, netplan, ssh, inotify, istio)
      4) Deploy Helm releases on WORKLOAD cluster (from YAML config)
    """
    typer.echo(f"Workspace root: {WORKSPACE_ROOT}")
    typer.echo(config)

    observers = [ConsoleObserver()]

    # --- 1) Cluster API phase (management cluster) ---
    if not skip_clusterapi:
        typer.echo("\n[clusterapi] Bootstrapping Cluster API components...")

        # Load config
        cfg = load_config(config)
        cluster_name = getattr(cfg, "name", "unnamed-cluster")

        # Define workspace and artifact path
        artifacts_dir = Path(WORKSPACE_ROOT) / "artifacts" / "clusterapi"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = artifacts_dir / f"{cluster_name}-manifest.yaml"

        # Generate full YAML manifest content
        manager = ClusterAPIManager(WORKSPACE_ROOT, mgmt_context=mgmt_context, observers=observers)
        manifest_data = manager.render_dynamic(cfg)  # <-- this assumes you can render YAML without applying yet

        # Save the manifest to file
        with open(manifest_path, "w") as f:
            f.write(manifest_data)

        typer.echo(f"[clusterapi] Manifest saved to {manifest_path}")

        # Apply it after saving
        manager.deploy_dynamic(cfg)
        typer.echo("[clusterapi] Waiting 10 seconds for workload kubeconfig to become available...")
        time.sleep(10)
    else:
        typer.echo("[clusterapi] Skipped.")

    # --- 2) Setup phase (management + workload) ---
    if not skip_setup:
        typer.echo("\n[setup] Running setup phase on management/workload clusters...")
        setup = SetupManager(WORKSPACE_ROOT, mgmt_context=mgmt_context)
        setup.run(SetupOptions(cluster_name=cfg.cluster_api.cluster_name))
    else:
        typer.echo("[setup] Skipped.")

    # --- 3) Node OS bootstrap via SSH ---
    if not skip_nodes:
        typer.echo("\n[nodes] Bootstrapping node OS roles via SSH...")

        inv = inventory_path(WORKSPACE_ROOT)
        pairs = read_hosts_from_inventory(inv)
        if not pairs:
            typer.echo(f"[nodes] No hosts found in {inv}. Skipping node bootstrap.")
        else:
            hosts: List[Host] = []
            for hostname, address in pairs:
                hosts.append(
                    Host(
                        hostname=hostname,
                        address=address,
                        username=ssh_username,
                        password=ssh_password,
                        pkey_path=ssh_key,
                        authorized_key_path=(Path.home() / ".ssh" / "openstack-key.pub")
                        if (Path.home() / ".ssh" / "openstack-key.pub").exists()
                        else None,
                    )
                )

            plan = plan_from_tags(node_tags)
            kubeconfig_text = maybe_read_kubeconfig_text()  # from Setup; else bootstrapper fetches via clusterctl
            opts = NodeBootstrapOptions(
                cluster_name=cluster_name,
                kubeconfig_content=kubeconfig_text,
                domain_suffix=domain_suffix,
                managed_user=managed_user,
                managed_user_password_plain=managed_user_password,
                # netplan_renderer can be provided here if needed
            )

            SshBootstrapper().bootstrap(hosts, plan, opts)
            typer.echo("[nodes] Node OS bootstrap completed.")
    else:
        typer.echo("[nodes] Skipped.")

    # --- 4) Ceph phase (pre-Helm) ---
    if not skip_ceph:
        typer.echo("\n[ceph] Deploying Ceph via cephadm (mon/mgr/osd)...")

        inv = inventory_path(WORKSPACE_ROOT)
        ceph_pairs = read_group_from_inventory(inv, "cephs")
        if not ceph_pairs:
            typer.echo(f"[ceph] No [cephs] group found in {inv}; skipping Ceph.")
        else:
            ceph_hosts: List[CephHost] = [
                CephHost(hostname=h, address=a, username=ssh_username, password=ssh_password, pkey_path=str(ssh_key) if ssh_key else None)
                for (h, a) in ceph_pairs
            ]
            ceph_cfg = CephConfig(
                version=ceph_version,
                image=ceph_image,
                # you can expose these as flags later:
                initial_dashboard_user="admin",
                initial_dashboard_password="admin",
                apply_osds_all_devices=True,
                mgr_count=2,
                mon_count=None,  # infer min(3, len(hosts))
            )
            CephManager().deploy(ceph_hosts, ceph_cfg)
            typer.echo("[ceph] Ceph deployment completed.")
    else:
        typer.echo("[ceph] Skipped.")


    # --- 5) Helm phase (workload cluster) ---
    typer.echo("\n[helm] Starting Helm deployments on workload cluster...")
    cfg = load_config(config)
    helm = HelmCliRunner(kube_context=context or cfg.context, debug=debug)
    report = deploy_all(cfg, helm, options=DeployOptions(), observers=observers)

    typer.echo("\nDeployment summary:")
    typer.echo(report.summary())


if __name__ == "__main__":
    app()
