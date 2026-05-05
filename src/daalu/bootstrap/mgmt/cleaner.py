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
        workload_cluster_name: Optional[str] = None,
        workload_cluster_namespace: Optional[str] = None,
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

        # Derive cluster name/namespace from config when not explicitly provided.
        # cluster_api.cluster_name and cluster_api.namespace are the authoritative
        # source for CAPT deployments (namespace is "default", not "baremetal-operator-system").
        ca = getattr(self._cfg, "cluster_api", None)
        resolved_cluster_name = workload_cluster_name or (
            getattr(ca, "cluster_name", None) or "auto-openstack-infra"
        )
        resolved_cluster_ns = workload_cluster_namespace or (
            getattr(ca, "namespace", None) or "default"
        )

        # ------------------------------------------------------------------
        # 1. Delete workload CAPI cluster
        # ------------------------------------------------------------------
        if not skip_workload_cluster and kc and Path(kc).exists():
            self._delete_workload_cluster(
                kubeconfig=kc,
                cluster_name=resolved_cluster_name,
                namespace=resolved_cluster_ns,
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

                if kc and Path(kc).exists():
                    self._delete_pvcs(kc)

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

        # Give CAPT a short grace period to begin its own cleanup, then force-remove
        # all CAPI/CAPT finalizers immediately. We do not wait the full timeout before
        # forcing — in a wipe scenario graceful BMC power-off is not required, and
        # stuck finalizers (unreachable nodes, crashed controllers) would otherwise
        # block the clean for the entire timeout period.
        grace = min(30, timeout)
        log.info(
            "[clean] Waiting %ds for CAPI to begin deprovisioning '%s'...",
            grace, cluster_name,
        )
        deadline = time.time() + grace
        while time.time() < deadline:
            r = subprocess.run(
                [
                    "kubectl", "--kubeconfig", kubeconfig,
                    "get", "cluster", cluster_name, "-n", namespace,
                    "--ignore-not-found",
                    "-o", "jsonpath={.metadata.name}",
                ],
                capture_output=True, text=True,
            )
            if not r.stdout.strip():
                log.info("[clean] Cluster '%s' deleted cleanly during grace period", cluster_name)
                return
            time.sleep(5)

        log.info(
            "[clean] Cluster '%s' still present after grace period — "
            "force-removing CAPI/CAPT finalizers to unblock deletion",
            cluster_name,
        )
        self._force_clean_capi_objects(kubeconfig=kubeconfig, namespace=namespace)

        # Brief wait for Kubernetes to process the finalizer removals
        log.info("[clean] Waiting for Cluster CR to disappear after finalizer removal...")
        for _ in range(12):  # up to 60s
            r = subprocess.run(
                [
                    "kubectl", "--kubeconfig", kubeconfig,
                    "get", "cluster", cluster_name, "-n", namespace,
                    "--ignore-not-found",
                    "-o", "jsonpath={.metadata.name}",
                ],
                capture_output=True, text=True,
            )
            if not r.stdout.strip():
                log.info("[clean] Cluster '%s' deleted after finalizer removal", cluster_name)
                return
            time.sleep(5)

        log.warning("[clean] Cluster '%s' still present — continuing cleanup anyway", cluster_name)

    # ------------------------------------------------------------------
    # Force-clean CAPI objects when graceful deletion times out
    # ------------------------------------------------------------------

    def _force_clean_capi_objects(self, *, kubeconfig: str, namespace: str) -> None:
        """
        Forcibly remove CAPI/CAPT object finalizers and delete objects when
        the graceful CAPI deprovision loop times out.

        Handles: TinkerbellMachine, Machine, MachineDeployment, KubeadmControlPlane,
        KubeadmConfig, Cluster objects in the given namespace.
        """
        log.info("[clean] Force-cleaning CAPI objects in namespace '%s'...", namespace)

        # Resource types with CAPI/CAPT finalizers that can block deletion
        capi_resources = [
            "tinkerbellmachines",
            "machines.cluster.x-k8s.io",
            "machinedeployments",
            "kubeadmcontrolplanes",
            "kubeadmconfigs",
            "clusters.cluster.x-k8s.io",
        ]

        for resource in capi_resources:
            r = subprocess.run(
                [
                    "kubectl", "--kubeconfig", kubeconfig,
                    "get", resource, "-n", namespace,
                    "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
                    "--ignore-not-found",
                ],
                capture_output=True, text=True,
            )
            for name in r.stdout.strip().splitlines():
                if not name:
                    continue
                # Remove all finalizers
                subprocess.run(
                    [
                        "kubectl", "--kubeconfig", kubeconfig,
                        "patch", resource, name, "-n", namespace,
                        "--type=json",
                        "-p=[{\"op\":\"remove\",\"path\":\"/metadata/finalizers\"}]",
                    ],
                    capture_output=True, text=True,
                )
            # Delete all objects of this type
            subprocess.run(
                [
                    "kubectl", "--kubeconfig", kubeconfig,
                    "delete", resource, "-n", namespace, "--all",
                    "--ignore-not-found", "--wait=false",
                ],
                capture_output=True, text=True,
            )

        log.info("[clean] CAPI object force-clean complete")

    # ------------------------------------------------------------------
    # 1b. Wipe workload node disks (forces PXE boot on next power-on)
    # ------------------------------------------------------------------

    def _wipe_workload_nodes(self, mgmt_cfg) -> None:
        """
        SSH to each workload node and zero the first 100 MB of its boot disk.
        This destroys the MBR/GPT so the node falls through to PXE on next boot.

        Tries node_ssh_username (default: root) first, then managed_user (builder)
        as a fallback, since CAPI-provisioned nodes may only have the managed user.
        """
        for hw in mgmt_cfg.hardware:
            log.info(
                "[clean] Wiping boot disk %s on workload node %s (%s)...",
                hw.disk, hw.name, hw.ip,
            )
            # Build the key kwargs once — shared across both user attempts
            key_kwargs: dict = {}
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
                    key_kwargs["pkey"] = pkey
                else:
                    key_kwargs["key_filename"] = str(Path(mgmt_cfg.ssh_key).expanduser())
            else:
                key_kwargs["look_for_keys"] = True
                key_kwargs["allow_agent"] = True

            # Try primary username, fall back to managed_user (builder) if auth fails
            usernames = [mgmt_cfg.node_ssh_username]
            if mgmt_cfg.managed_user and mgmt_cfg.managed_user != mgmt_cfg.node_ssh_username:
                usernames.append(mgmt_cfg.managed_user)

            client = None
            connected_user = None
            connect_error = None
            for username in usernames:
                try:
                    c = paramiko.SSHClient()
                    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    c.connect(hostname=hw.ip, username=username, timeout=30, **key_kwargs)
                    client = c
                    connected_user = username
                    break
                except paramiko.ssh_exception.AuthenticationException:
                    log.debug(
                        "[clean] Auth failed as %s on %s — trying next user", username, hw.name,
                    )
                    connect_error = "auth_failed"
                except Exception as exc:
                    connect_error = str(exc)
                    break  # host unreachable — no point trying other usernames

            if client is None:
                if connect_error == "auth_failed":
                    log.warning(
                        "[clean] SSH auth failed on %s (%s) for users %s — skipping wipe",
                        hw.name, hw.ip, usernames,
                    )
                else:
                    log.info(
                        "[clean] %s (%s) unreachable (already wiped/rebooted?) — skipping wipe",
                        hw.name, hw.ip,
                    )
                continue

            log.debug("[clean] Connected to %s as %s", hw.name, connected_user)
            try:
                ssh = SSHRunner(client)
                rc, out, err = ssh.run(
                    f"dd if=/dev/zero of={hw.disk} bs=1M count=100 conv=noerror 2>&1",
                    sudo=(connected_user != "root"),
                )
                if rc == 0:
                    log.info("[clean] Disk %s wiped on %s — rebooting", hw.disk, hw.name)
                else:
                    log.warning("[clean] dd exited %d on %s: %s", rc, hw.name, err or out)
                ssh.run("reboot", sudo=(connected_user != "root"))
            finally:
                client.close()

    # ------------------------------------------------------------------
    # Tinkerbell teardown (runs before kubeadm reset, API server still up)
    # ------------------------------------------------------------------

    def _clean_tinkerbell(self, kubeconfig: str) -> None:
        """
        Tear down the Tinkerbell/CAPT stack from the mgmt cluster.

        Since all CAPI and Tinkerbell objects live in the 'tinkerbell' namespace,
        deletion order matters to avoid finalizer deadlocks:

          1. Force-remove finalizers and delete remaining CAPI objects
             (TinkerbellMachine, Machine, KCP, MachineDeployment, Cluster)
          2. Force-remove finalizers and delete Tinkerbell Hardware objects
          3. Delete Tinkerbell Workflow and Template objects
          4. Delete Rufio Machine and Job objects
          5. clusterctl delete --infrastructure tinkerbell (removes CAPT CRDs+controllers)
          6. helm uninstall tinkerbell -n tinkerbell (removes Tink/SMEE/Hegel/Rufio)
          7. Delete the tinkerbell namespace (catches any stragglers)
          8. clusterctl delete core/bootstrap/control-plane providers
        """

        def _remove_finalizers_and_delete(resource: str) -> None:
            """Patch finalizers off all instances of a resource (all namespaces), then delete."""
            r = subprocess.run(
                [
                    "kubectl", "--kubeconfig", kubeconfig,
                    "get", resource, "-A", "--ignore-not-found",
                    "-o", "jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}{'\\n'}{end}",
                ],
                capture_output=True, text=True,
            )
            for line in r.stdout.strip().splitlines():
                if "/" not in line:
                    continue
                ns, name = line.split("/", 1)
                subprocess.run(
                    [
                        "kubectl", "--kubeconfig", kubeconfig,
                        "patch", resource, name, "-n", ns,
                        "--type=json",
                        "-p=[{\"op\":\"remove\",\"path\":\"/metadata/finalizers\"}]",
                    ],
                    capture_output=True, text=True,
                )
            subprocess.run(
                [
                    "kubectl", "--kubeconfig", kubeconfig,
                    "delete", resource, "-A", "--all",
                    "--ignore-not-found", "--wait=false",
                ],
                capture_output=True, text=True,
            )

        # 1. CAPI objects (TinkerbellMachine has a CAPT finalizer; Cluster/Machine have CAPI finalizers)
        log.info("[clean] Force-removing CAPI/CAPT object finalizers and deleting...")
        for res in [
            "tinkerbellmachines",
            "machines.cluster.x-k8s.io",
            "machinedeployments.cluster.x-k8s.io",
            "kubeadmcontrolplanes",
            "kubeadmconfigs",
            "clusters.cluster.x-k8s.io",
        ]:
            _remove_finalizers_and_delete(res)

        # 2. Hardware CRs (may have tinkerbellmachine finalizer from CAPT)
        log.info("[clean] Removing finalizers from Hardware objects and deleting...")
        _remove_finalizers_and_delete("hardware.tinkerbell.org")

        # 3. Tinkerbell Workflows and Templates
        log.info("[clean] Deleting Tinkerbell Workflow and Template objects...")
        for res in ["workflows.tinkerbell.org", "templates.tinkerbell.org"]:
            subprocess.run(
                ["kubectl", "--kubeconfig", kubeconfig,
                 "delete", res, "-A", "--all", "--ignore-not-found"],
                capture_output=True, text=True,
            )

        # 4. Rufio Jobs and Machines
        log.info("[clean] Deleting Rufio Job and Machine objects...")
        for res in ["jobs.bmc.tinkerbell.org", "machines.bmc.tinkerbell.org"]:
            _remove_finalizers_and_delete(res)

        # 5. Remove CAPT infrastructure provider (deletes CAPT controller + CRDs)
        log.info("[clean] Removing CAPT infrastructure provider via clusterctl...")
        subprocess.run(
            ["clusterctl", "--kubeconfig", kubeconfig,
             "delete", "--infrastructure", "tinkerbell"],
            capture_output=True,
        )

        # 6. Helm uninstall Tinkerbell stack
        log.info("[clean] Uninstalling Tinkerbell Helm release...")
        subprocess.run(
            ["helm", "--kubeconfig", kubeconfig,
             "uninstall", "tinkerbell", "-n", "tinkerbell"],
            capture_output=True,
        )

        # 7. Remove CAPI core providers
        log.info("[clean] Removing CAPI core providers via clusterctl...")
        subprocess.run(
            ["clusterctl", "--kubeconfig", kubeconfig,
             "delete", "--all"],
            capture_output=True,
        )

        # 8. Force-delete tinkerbell namespace (catches any CRs still stuck with finalizers)
        log.info("[clean] Force-deleting tinkerbell namespace...")
        # First patch any remaining CRs' finalizers in the namespace
        for res in ["hardware", "workflows", "templates", "machines", "jobs"]:
            r = subprocess.run(
                [
                    "kubectl", "--kubeconfig", kubeconfig,
                    "get", res, "-n", "tinkerbell", "--ignore-not-found",
                    "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
                ],
                capture_output=True, text=True,
            )
            for name in r.stdout.strip().splitlines():
                if not name:
                    continue
                subprocess.run(
                    [
                        "kubectl", "--kubeconfig", kubeconfig,
                        "patch", res, name, "-n", "tinkerbell",
                        "--type=json",
                        "-p=[{\"op\":\"remove\",\"path\":\"/metadata/finalizers\"}]",
                    ],
                    capture_output=True, text=True,
                )
        subprocess.run(
            ["kubectl", "--kubeconfig", kubeconfig,
             "delete", "namespace", "tinkerbell", "--ignore-not-found"],
            capture_output=True,
        )

        log.info("[clean] Tinkerbell teardown complete")

    # ------------------------------------------------------------------
    # Step 2 — Delete all PVCs (before kubeadm reset kills the API server)
    # ------------------------------------------------------------------

    def _delete_pvcs(self, kubeconfig: str) -> None:
        """Delete all PVCs in all namespaces so local-path-provisioner hostPath
        directories are cleaned up and don't fill the disk."""
        log.info("[clean] Removing finalizers from PVCs...")
        r = subprocess.run(
            [
                "kubectl", "--kubeconfig", kubeconfig,
                "get", "pvc", "-A",
                "-o", "jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}{'\\n'}{end}",
            ],
            capture_output=True, text=True,
        )
        for line in r.stdout.strip().splitlines():
            if "/" not in line:
                continue
            ns, name = line.split("/", 1)
            subprocess.run(
                [
                    "kubectl", "--kubeconfig", kubeconfig,
                    "patch", "pvc", name, "-n", ns,
                    "--type=json",
                    "-p=[{\"op\":\"remove\",\"path\":\"/metadata/finalizers\"}]",
                ],
                capture_output=True, text=True,
            )

        log.info("[clean] Deleting all PVCs across all namespaces...")
        result = subprocess.run(
            [
                "kubectl", "--kubeconfig", kubeconfig,
                "delete", "pvc", "--all", "--all-namespaces",
                "--ignore-not-found",
                "--wait=false",
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.warning("[clean] PVC deletion returned non-zero: %s", result.stderr.strip())
        else:
            log.info("[clean] PVCs deleted: %s", result.stdout.strip() or "(none found)")

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
        ssh.run("rm -rf /var/lib/rancher/local-path-provisioner /opt/local-path-provisioner 2>/dev/null || true", sudo=True)

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
