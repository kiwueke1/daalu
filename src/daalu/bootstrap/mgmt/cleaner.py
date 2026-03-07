# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/mgmt/cleaner.py

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

import paramiko

from daalu.bootstrap.mgmt.models import MgmtClusterConfig
from daalu.config.models import DaaluConfig, RegistryConfig
from daalu.utils.ssh_runner import SSHRunner

log = logging.getLogger("daalu")


class MgmtClusterCleaner:
    """
    Tears down everything daalu created:

      1. Delete the workload CAPI cluster (so Metal3/Ironic can deprovision
         bare-metal hosts cleanly before we destroy the mgmt cluster).
      2. SSH to the mgmt node and run kubeadm reset + CNI/k8s cleanup.
      3. Wipe and unmount the Harbor disk; remove fstab entry.
      4. Remove local kubeconfig files and known_hosts entries.
    """

    def __init__(self, cfg: DaaluConfig):
        self._cfg = cfg

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def clean(
        self,
        *,
        mgmt_kubeconfig: Optional[str] = None,
        workload_cluster_name: str = "auto-openstack-infra",
        workload_cluster_namespace: str = "baremetal-operator-system",
        skip_workload_cluster: bool = False,
        wait_deprovision: bool = True,
        deprovision_timeout: int = 300,
    ) -> None:
        mgmt_cfg = self._cfg.mgmt_cluster
        registry_cfg = self._cfg.registry

        kc = mgmt_kubeconfig or (
            str(Path(mgmt_cfg.kubeconfig_output_path).expanduser())
            if mgmt_cfg
            else None
        )

        # ------------------------------------------------------------------
        # 1. Delete workload CAPI cluster
        # ------------------------------------------------------------------
        if not skip_workload_cluster and kc and Path(kc).exists():
            self._delete_workload_cluster(
                kubeconfig=kc,
                cluster_name=workload_cluster_name,
                namespace=workload_cluster_namespace,
                wait=wait_deprovision,
                timeout=deprovision_timeout,
            )
        else:
            if skip_workload_cluster:
                log.info("[clean] Skipping workload cluster deletion (--skip-workload-cluster)")
            else:
                log.info("[clean] No mgmt kubeconfig found — skipping workload cluster deletion")

        # ------------------------------------------------------------------
        # 2. Reset mgmt node (k8s + Harbor disk + Metal3)
        # ------------------------------------------------------------------
        if mgmt_cfg:
            client = self._ssh_connect(mgmt_cfg)
            try:
                ssh = SSHRunner(client)
                self._reset_kubernetes(ssh)
                self._clean_harbor_disk(ssh, registry_cfg)
                self._clean_metal3(ssh)
            finally:
                client.close()
        else:
            log.warning("[clean] No mgmt_cluster config — skipping remote cleanup")

        # ------------------------------------------------------------------
        # 3. Clean up local state
        # ------------------------------------------------------------------
        self._clean_local(kc, mgmt_cfg)

        log.info("[clean] Teardown complete")

    # ------------------------------------------------------------------
    # Step 1 — Delete workload CAPI cluster
    # ------------------------------------------------------------------

    def _delete_workload_cluster(
        self,
        *,
        kubeconfig: str,
        cluster_name: str,
        namespace: str,
        wait: bool,
        timeout: int,
    ) -> None:
        log.info(
            "[clean] Deleting workload cluster '%s' in namespace '%s'...",
            cluster_name, namespace,
        )

        result = subprocess.run(
            [
                "kubectl", "--kubeconfig", kubeconfig,
                "delete", "cluster", cluster_name,
                "-n", namespace,
                "--ignore-not-found",
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.warning("[clean] kubectl delete cluster returned non-zero: %s", result.stderr.strip())
        else:
            log.info("[clean] Delete command accepted: %s", result.stdout.strip())

        if not wait:
            return

        log.info(
            "[clean] Waiting up to %ds for BareMetalHosts to reach 'available' state...",
            timeout,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = subprocess.run(
                [
                    "kubectl", "--kubeconfig", kubeconfig,
                    "get", "bmh", "-A",
                    "-o", "jsonpath={.items[*].status.provisioning.state}",
                ],
                capture_output=True, text=True,
            )
            states = r.stdout.strip().split()
            if not states:
                log.info("[clean] No BareMetalHosts found — deprovisioning complete")
                return
            non_available = [s for s in states if s not in ("available", "")]
            if not non_available:
                log.info("[clean] All BareMetalHosts are 'available' — deprovisioning complete")
                return
            log.info("[clean] BMH states: %s — still waiting...", states)
            time.sleep(15)

        log.warning(
            "[clean] Timed out waiting for BMH deprovisioning. "
            "Proceeding with mgmt cluster teardown anyway."
        )

    # ------------------------------------------------------------------
    # Step 2a — kubeadm reset + k8s cleanup
    # ------------------------------------------------------------------

    def _reset_kubernetes(self, ssh: SSHRunner) -> None:
        log.info("[clean] Running kubeadm reset on mgmt node...")
        ssh.run("kubeadm reset -f 2>/dev/null || true", sudo=True)

        log.info("[clean] Removing k8s data directories...")
        ssh.run(
            "rm -rf /etc/cni/net.d /var/lib/cni /var/lib/etcd "
            "/var/lib/kubelet /etc/kubernetes",
            sudo=True,
        )
        ssh.run("rm -rf ~/.kube /root/.kube", sudo=True)

        log.info("[clean] Flushing iptables and removing CNI/Cilium interfaces...")
        ssh.run("iptables -F; iptables -t nat -F; iptables -t mangle -F; iptables -X", sudo=True)
        ssh.run("ipvsadm --clear 2>/dev/null || true", sudo=True)
        for iface in ("cni0", "flannel.1", "cilium_host", "cilium_net", "lxc_health"):
            ssh.run(f"ip link delete {iface} 2>/dev/null || true", sudo=True)

        log.info("[clean] Kubernetes reset complete")

    # ------------------------------------------------------------------
    # Step 2b — Harbor disk cleanup
    # ------------------------------------------------------------------

    def _clean_harbor_disk(self, ssh: SSHRunner, registry_cfg: Optional[RegistryConfig]) -> None:
        mount_path = "/mnt/harbor-storage"
        disk_device = None

        if registry_cfg:
            mount_path = registry_cfg.storage_mount_path or mount_path
            disk_device = registry_cfg.disk_device

        log.info("[clean] Unmounting Harbor storage at %s...", mount_path)
        ssh.run(f"umount {mount_path} 2>/dev/null || true", sudo=True)
        escaped = mount_path.replace("/", "\\/")
        ssh.run(f"sed -i '/{escaped}/d' /etc/fstab", sudo=True)
        ssh.run(f"rm -rf {mount_path}", sudo=True)

        if disk_device:
            log.info("[clean] Wiping Harbor disk %s...", disk_device)
            ssh.run(f"wipefs -a {disk_device}", sudo=True)
        else:
            log.info("[clean] No disk_device configured — skipping wipefs")

        log.info("[clean] Removing local-path-provisioner data...")
        ssh.run("rm -rf /var/lib/rancher/local-path-provisioner 2>/dev/null || true", sudo=True)

    # ------------------------------------------------------------------
    # Step 2c — Metal3 / Ironic / Docker cleanup
    # ------------------------------------------------------------------

    def _clean_metal3(self, ssh: SSHRunner) -> None:
        log.info("[clean] Cleaning up Metal3/Ironic state...")
        ssh.run("rm -rf /opt/metal3-dev-env 2>/dev/null || true", sudo=True)

        # Stop and remove all Docker containers left by IrSO/cephadm
        rc, out, _ = ssh.run("docker ps -aq 2>/dev/null", sudo=True)
        if rc == 0 and out.strip():
            ssh.run("docker rm -f $(docker ps -aq) 2>/dev/null || true", sudo=True)
        ssh.run("docker volume prune -f 2>/dev/null || true", sudo=True)

    # ------------------------------------------------------------------
    # Step 3 — Local cleanup
    # ------------------------------------------------------------------

    def _clean_local(
        self,
        kubeconfig: Optional[str],
        mgmt_cfg: Optional[MgmtClusterConfig],
    ) -> None:
        log.info("[clean] Cleaning up local state...")

        if kubeconfig:
            kc_path = Path(kubeconfig).expanduser()
            if kc_path.exists():
                kc_path.unlink()
                log.info("[clean] Removed kubeconfig: %s", kc_path)

        # Remove workload kubeconfig written to /tmp by deploy
        for tmp_kc in Path("/tmp").glob("kubeconfig-*.yaml"):
            tmp_kc.unlink(missing_ok=True)
            log.info("[clean] Removed %s", tmp_kc)

        # Remove mgmt node from known_hosts
        if mgmt_cfg:
            for host in (mgmt_cfg.host, mgmt_cfg.provisioning_ip):
                if host:
                    subprocess.run(
                        ["ssh-keygen", "-R", host],
                        capture_output=True,
                    )
                    log.info("[clean] Removed %s from known_hosts", host)

    # ------------------------------------------------------------------
    # SSH helper (mirrors MgmtClusterManager._ssh_connect)
    # ------------------------------------------------------------------

    def _ssh_connect(self, cfg: MgmtClusterConfig) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict = {
            "hostname": cfg.host,
            "username": cfg.ssh_username,
            "timeout": 30,
        }

        if cfg.ssh_key:
            pkey = None
            for key_class in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                try:
                    pkey = key_class.from_private_key_file(
                        str(Path(cfg.ssh_key).expanduser())
                    )
                    break
                except paramiko.ssh_exception.SSHException:
                    continue
            if pkey:
                connect_kwargs["pkey"] = pkey
            else:
                connect_kwargs["key_filename"] = str(Path(cfg.ssh_key).expanduser())
        elif cfg.ssh_password:
            connect_kwargs["password"] = cfg.ssh_password
        else:
            connect_kwargs["look_for_keys"] = True
            connect_kwargs["allow_agent"] = True

        client.connect(**connect_kwargs)
        log.info("[clean] SSH connected to %s", cfg.host)
        return client
