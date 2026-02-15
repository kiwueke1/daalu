# src/daalu/temporal/activities.py

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Optional, List

from temporalio import activity

from daalu.config.loader import load_config

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

from daalu.bootstrap.ceph.models import CephHost
from daalu.utils.ssh_runner import SSHRunner
from daalu.helm.cli_runner import HelmCliRunner
import paramiko

from .models import DeployRequest

# --- helper: connect controller the same way your CLI does ---
def _connect_controller_ssh(*, workspace_root: Path, managed_user: str,
                            ssh_key: Optional[str], ssh_password: Optional[str]) -> paramiko.SSHClient:
    # import here to avoid circular import if needed
    from daalu.cli.app import connect_controller_ssh
    client, _host = connect_controller_ssh(
        workspace_root=workspace_root,
        managed_user=managed_user,
        ssh_key=Path(ssh_key) if ssh_key else None,
        ssh_password=ssh_password,
    )
    return client


@activity.defn
def activity_deploy_cluster_api(req: DeployRequest) -> None:
    cfg = load_config(req.config_path)
    provider = getattr(cfg.cluster_api, "provider", "proxmox")
    workspace_root = Path(req.workspace_root)

    if provider == "metal3":
        deploy_cluster_api_metal3(
            cfg=cfg,
            workspace_root=workspace_root,
            mgmt_context=req.mgmt_context,
            dry_run=req.dry_run,
        )
    else:
        deploy_cluster_api_generic(
            cfg=cfg,
            workspace_root=workspace_root,
            mgmt_context=req.mgmt_context,
        )


@activity.defn
def activity_deploy_nodes(req: DeployRequest) -> None:
    cfg = load_config(req.config_path)
    deploy_nodes(
        cfg=cfg,
        workspace_root=Path(req.workspace_root),
        cluster_name=req.cluster_name,
        node_tags=req.node_tags,
        ssh_username=req.ssh_username,
        ssh_key=Path(req.ssh_key) if req.ssh_key else None,
        domain_suffix=req.domain_suffix,
        managed_user=req.managed_user,
        managed_user_password=req.managed_user_password,
    )


@activity.defn
def activity_deploy_ceph(req: DeployRequest) -> List[CephHost]:
    # returns Ceph hosts so CSI can use them
    return deploy_ceph(
        workspace_root=Path(req.workspace_root),
        managed_user=req.managed_user,
        ssh_key=Path(req.ssh_key) if req.ssh_key else None,
        ceph_version=req.ceph_version,
        ceph_image=req.ceph_image,
    )


@activity.defn
def activity_deploy_csi(req: DeployRequest, ceph_hosts: List[CephHost]) -> None:
    cfg = load_config(req.config_path)

    client = _connect_controller_ssh(
        workspace_root=Path(req.workspace_root),
        managed_user=req.managed_user,
        ssh_key=req.ssh_key,
        ssh_password=req.ssh_password,
    )
    try:
        ssh = SSHRunner(client)
        helm = HelmCliRunner(ssh=ssh, kube_context=req.context or cfg.context)

        kubeconfig_path = f"/tmp/kubeconfig-{cfg.cluster_api.cluster_name}.yaml"
        ssh.put_file(local_path=kubeconfig_path, remote_path=kubeconfig_path)

        deploy_csi(
            helm=helm,
            ceph_hosts=ceph_hosts,
            kubeconfig_path=kubeconfig_path,
        )
    finally:
        client.close()


@activity.defn
def activity_deploy_infrastructure(req: DeployRequest) -> None:
    cfg = load_config(req.config_path)

    client = _connect_controller_ssh(
        workspace_root=Path(req.workspace_root),
        managed_user=req.managed_user,
        ssh_key=req.ssh_key,
        ssh_password=req.ssh_password,
    )
    try:
        ssh = SSHRunner(client)
        helm = HelmCliRunner(ssh=ssh, kube_context=req.context or cfg.context)

        kubeconfig_path = f"/tmp/kubeconfig-{cfg.cluster_api.cluster_name}.yaml"
        ssh.put_file(local_path=kubeconfig_path, remote_path=kubeconfig_path)

        deploy_infrastructure(
            helm=helm,
            ssh=ssh,
            workspace_root=Path(req.workspace_root),
            infra_flag=req.infra,
            kubeconfig_path=kubeconfig_path,
        )
    finally:
        client.close()
