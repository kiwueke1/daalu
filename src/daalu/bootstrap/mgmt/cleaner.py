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
        wipe_mgmt: bool = False,
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
        # 1b. Wipe workload node disks so they PXE-boot on next power-on
        # ------------------------------------------------------------------
        if mgmt_cfg and mgmt_cfg.hardware:
            self._wipe_workload_nodes(mgmt_cfg)

        # ------------------------------------------------------------------
        # 2. Reset mgmt node (k8s + Harbor disk + provider stack)
        # ------------------------------------------------------------------
        if wipe_mgmt:
            if mgmt_cfg:
                from daalu.bootstrap.mgmt.models import BaremetalProvider
                provider = mgmt_cfg.provider

                # Provider-specific teardown runs against the mgmt cluster
                # before we wipe k8s, while the API server is still up.
                if kc and Path(kc).exists():
                    if provider == BaremetalProvider.tinkerbell:
                        self._clean_tinkerbell(kc)
                    elif provider == BaremetalProvider.metal3:
                        pass  # Metal3 is cleaned as part of _clean_metal3 below

                client = self._ssh_connect(mgmt_cfg)
                try:
                    ssh = SSHRunner(client)
                    self._reset_kubernetes(ssh)
                    self._clean_harbor_disk(ssh, registry_cfg)
                    if provider == BaremetalProvider.metal3:
                        self._clean_metal3(ssh)
                finally:
                    client.close()
            else:
                log.warning("[clean] No mgmt_cluster config — skipping remote cleanup")
        else:
            log.info(
                "[clean] Skipping mgmt node reset. "
                "Use --wipe-mgmt to also destroy the management cluster."
            )

        # ------------------------------------------------------------------
        # 3. Clean up local state
        # ------------------------------------------------------------------
        self._clean_local(kc, mgmt_cfg, wipe_mgmt=wipe_mgmt)

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
    # 1b. Wipe workload node disks (forces PXE boot on next power-on)
    # ------------------------------------------------------------------

    def _wipe_workload_nodes(self, mgmt_cfg) -> None:
        """
        SSH to each workload node and zero the first 100 MB of its boot disk.
        This destroys the MBR/GPT so the node falls through to PXE on next boot.

        Uses the same SSH key as the mgmt cluster but connects as node_ssh_username
        (default: root) since workload nodes are provisioned with root access.
        """
        for hw in mgmt_cfg.hardware:
            log.info(
                "[clean] Wiping boot disk %s on workload node %s (%s)...",
                hw.disk, hw.name, hw.ip,
            )
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                connect_kwargs: dict = {
                    "hostname": hw.ip,
                    "username": mgmt_cfg.node_ssh_username,
                    "timeout": 30,
                }

                if mgmt_cfg.ssh_key:
                    pkey = None
                    for key_class in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                        try:
                            pkey = key_class.from_private_key_file(
                                str(Path(mgmt_cfg.ssh_key).expanduser())
                            )
                            break
                        except paramiko.ssh_exception.SSHException:
                            continue
                    if pkey:
                        connect_kwargs["pkey"] = pkey
                    else:
                        connect_kwargs["key_filename"] = str(Path(mgmt_cfg.ssh_key).expanduser())
                else:
                    connect_kwargs["look_for_keys"] = True
                    connect_kwargs["allow_agent"] = True

                client.connect(**connect_kwargs)
                ssh = SSHRunner(client)
                try:
                    rc, out, err = ssh.run(
                        f"dd if=/dev/zero of={hw.disk} bs=1M count=100 conv=noerror 2>&1"
                    )
                    if rc == 0:
                        log.info("[clean] Disk %s wiped on %s — rebooting", hw.disk, hw.name)
                    else:
                        log.warning("[clean] dd exited %d on %s: %s", rc, hw.name, err or out)
                    # Reboot regardless — best-effort
                    ssh.run("reboot")
                finally:
                    client.close()
            except Exception:
                log.exception("[clean] Failed to wipe node %s (%s) — skipping", hw.name, hw.ip)

    # ------------------------------------------------------------------
    # Tinkerbell teardown (runs before kubeadm reset, API server still up)
    # ------------------------------------------------------------------

    def _clean_tinkerbell(self, kubeconfig: str) -> None:
        """
        Tear down the Tinkerbell stack from the mgmt cluster.

        Steps:
          1. Wait for Hardware CRs to reach 'provisioned' → 'available'
          2. helm uninstall tinkerbell -n tinkerbell
          3. kubectl delete namespace tinkerbell
          4. clusterctl delete --infrastructure tinkerbell
        """
        log.info("[clean] Deleting Tinkerbell Hardware objects...")
        subprocess.run(
            [
                "kubectl", "--kubeconfig", kubeconfig,
                "delete", "hardware", "-A", "--all",
                "--ignore-not-found",
            ],
            capture_output=True, text=True,
        )
        log.info("[clean] Hardware objects deleted")

        log.info("[clean] Uninstalling Tinkerbell Helm release...")
        subprocess.run(
            ["helm", "--kubeconfig", kubeconfig,
             "uninstall", "tinkerbell", "-n", "tinkerbell"],
            capture_output=True,
        )

        log.info("[clean] Deleting tinkerbell namespace...")
        subprocess.run(
            ["kubectl", "--kubeconfig", kubeconfig,
             "delete", "namespace", "tinkerbell", "--ignore-not-found"],
            capture_output=True,
        )

        log.info("[clean] Removing CAPT infrastructure provider...")
        subprocess.run(
            ["clusterctl", "--kubeconfig", kubeconfig,
             "delete", "--infrastructure", "tinkerbell"],
            capture_output=True,
        )

        log.info("[clean] Tinkerbell teardown complete")

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
        wipe_mgmt: bool = False,
    ) -> None:
        log.info("[clean] Cleaning up local state...")

        # Only remove the mgmt kubeconfig if we're also wiping the mgmt cluster —
        # if the mgmt cluster is still running the kubeconfig is still needed.
        if wipe_mgmt and kubeconfig:
            kc_path = Path(kubeconfig).expanduser()
            if kc_path.exists():
                kc_path.unlink()
                log.info("[clean] Removed kubeconfig: %s", kc_path)

        # Remove workload kubeconfig written to /tmp by deploy (always safe to remove)
        for tmp_kc in Path("/tmp").glob("kubeconfig-*.yaml"):
            tmp_kc.unlink(missing_ok=True)
            log.info("[clean] Removed %s", tmp_kc)

        # Remove mgmt node from known_hosts only if wiping mgmt
        if wipe_mgmt and mgmt_cfg:
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
