# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/registry/harbor_deployer.py

from __future__ import annotations

import base64
import json
import logging
import shlex
import subprocess
import time
from pathlib import Path

import paramiko
import requests
import urllib3
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("daalu")

HARBOR_REPO_NAME = "harbor"
HARBOR_REPO_URL = "https://helm.goharbor.io"
HARBOR_CHART = "harbor/harbor"

# Rancher local-path-provisioner — provides a "local-path" StorageClass that
# dynamically provisions directories on a node's filesystem.
LOCAL_PATH_PROVISIONER_URL = (
    "https://raw.githubusercontent.com/rancher/local-path-provisioner"
    "/v0.0.28/deploy/local-path-storage.yaml"
)


class HarborDeployer:
    """
    Deploys Harbor to the mgmt cluster via local subprocess helm invocations.

    Storage:
      If disk_device is set (e.g. "/dev/sda"), the deployer will format the
      disk (if no filesystem exists), mount it, deploy local-path-provisioner,
      and configure all Harbor PVCs to use the "local-path" StorageClass.

    Exposure:
      Harbor is accessed via kubectl port-forward or NodePort — no LoadBalancer
      or Istio required on the mgmt cluster.
    """

    def __init__(
        self,
        *,
        mgmt_kubeconfig: str,
        harbor_hostname: str,
        admin_password: str,
        namespace: str = "harbor",
        storage_size: str = "100Gi",
        values_path: Path | None = None,
        harbor_project: str = "openstack",
        local_chart_dir: Path | None = None,
        disk_device: str | None = None,
        storage_mount_path: str = "/mnt/harbor-storage",
        storage_class: str = "local-path",
        ssh_key: str | None = None,
        ssh_username: str = "ubuntu",
        ssh_password: str | None = None,
        harbor_node_ip: str | None = None,
    ):
        self.mgmt_kubeconfig = mgmt_kubeconfig
        self.harbor_hostname = harbor_hostname
        self.admin_password = admin_password
        self.namespace = namespace
        self.storage_size = storage_size
        self.values_path = values_path
        self.harbor_project = harbor_project
        self.local_chart_dir = local_chart_dir
        self.disk_device = disk_device
        self.storage_mount_path = storage_mount_path
        self.storage_class = storage_class
        self.ssh_key = ssh_key
        self.ssh_username = ssh_username
        self.ssh_password = ssh_password
        # When set, used as the NodePort access IP (and TLS cert SAN) instead
        # of auto-detecting from kubectl get nodes. Set to the provisioning NIC
        # IP (e.g. "10.10.0.9") to avoid using the mgmt NIC that OVN will claim.
        self.harbor_node_ip = harbor_node_ip
        # Set during deploy() to the actual reachable host:port
        self._access_url: str | None = None

    def deploy(self) -> None:
        log.info("[registry] Deploying Harbor to mgmt cluster...")
        self._ensure_namespace()

        already_deployed = self._helm_is_deployed()
        if not already_deployed:
            if not self.local_chart_dir:
                self._helm_add_repo()
            if self.disk_device:
                self._setup_disk_storage()
            self._cleanup_orphaned_pvcs()
        else:
            log.info("[registry] Harbor already deployed — upgrading with current values...")

        # Always run upgrade --install so updated values (e.g. redis config)
        # are applied and the Redis ConfigMap checksum triggers a rolling restart.
        self._helm_install_or_upgrade()
        self._wait_ready()

        # local-path-provisioner creates hostPath dirs as root:root 755.
        # Kubernetes fsGroup doesn't reliably chown HostPath-backed volumes, so
        # Harbor's registry and jobservice pods (uid 10000) can't write to their
        # storage. Fix this via a privileged Job running on the node.
        self._fix_storage_permissions()

        self._configure_redis()
        self._access_url = self._get_harbor_access_url()
        self._ensure_harbor_project()
        self._configure_containerd_on_mgmt_node()
        log.info("[registry] Harbor ready at https://%s", self._access_url)

    def _helm_is_deployed(self) -> bool:
        """Return True if the Harbor Helm release is in 'deployed' state."""
        result = subprocess.run(
            ["helm", "--kubeconfig", self.mgmt_kubeconfig,
             "list", "-n", self.namespace,
             "--filter", "^harbor$",
             "-o", "json"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return False
        try:
            releases = json.loads(result.stdout)
            return any(
                r.get("name") == "harbor" and r.get("status") == "deployed"
                for r in releases
            )
        except Exception:
            return False

    def get_registry_url(self) -> str:
        return self._access_url or self.harbor_hostname

    def _get_harbor_access_url(self) -> str:
        """
        Return the host:port Harbor is reachable at.
        Uses the fixed NodePort (30003) configured in the Helm values.
        Falls back to dynamic port discovery if the service reports differently.
        """
        node_ip = self._get_mgmt_node_ip()

        # Verify the HTTPS nodePort is actually 30003 (sanity check)
        result = subprocess.run(
            ["kubectl", "--kubeconfig", self.mgmt_kubeconfig,
             "get", "svc", "harbor", "-n", self.namespace,
             "-o", "jsonpath={.spec.ports[?(@.port==443)].nodePort}"],
            capture_output=True, text=True, check=False,
        )
        nodeport = result.stdout.strip() or "30003"
        access = f"{node_ip}:{nodeport}"
        log.info("[registry] Harbor accessible via NodePort: https://%s", access)
        return access

    # ------------------------------------------------------------------
    # kubectl / helm helpers
    # ------------------------------------------------------------------

    def _kubectl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["kubectl", "--kubeconfig", self.mgmt_kubeconfig, *args],
            check=check,
        )

    def _kubectl_input(self, *args: str, input: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["kubectl", "--kubeconfig", self.mgmt_kubeconfig, *args],
            input=input, text=True, check=check,
        )

    def _helm(self, *args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["helm", "--kubeconfig", self.mgmt_kubeconfig, *args],
            input=input, text=True, check=check,
        )

    # ------------------------------------------------------------------
    # Disk storage setup (runs on the mgmt k8s node via SSH)
    # ------------------------------------------------------------------

    def _get_mgmt_node_ip(self) -> str:
        """Return the IP to use for Harbor NodePort access.

        If harbor_node_ip is configured (recommended: set to the provisioning NIC
        IP, e.g. "10.10.0.9"), that is returned directly without querying kubectl.
        Otherwise falls back to auto-detecting the first node's InternalIP —
        which may be the wrong NIC if the node has multiple interfaces.
        """
        if self.harbor_node_ip:
            log.info("[registry] Using configured harbor_node_ip: %s", self.harbor_node_ip)
            return self.harbor_node_ip

        result = subprocess.run(
            [
                "kubectl", "--kubeconfig", self.mgmt_kubeconfig,
                "get", "nodes",
                "-o", "jsonpath={.items[0].status.addresses[?(@.type==\"InternalIP\")].address}",
            ],
            capture_output=True, text=True, check=True,
        )
        ip = result.stdout.strip()
        if not ip:
            raise RuntimeError(
                "Could not determine mgmt node IP from 'kubectl get nodes'. "
                "Ensure the mgmt cluster is reachable and has at least one node."
            )
        log.info("[registry] Mgmt node IP (auto-detected): %s", ip)
        return ip

    def _ssh_connect(self, ip: str) -> paramiko.SSHClient:
        """Open an SSH connection to the mgmt node (key or password auth)."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict = {
            "hostname": ip,
            "port": 22,
            "username": self.ssh_username,
            "timeout": 30,
        }
        if self.ssh_key:
            pkey = None
            for key_class in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                try:
                    pkey = key_class.from_private_key_file(str(self.ssh_key))
                    break
                except paramiko.ssh_exception.SSHException:
                    continue
            if pkey:
                connect_kwargs["pkey"] = pkey
            else:
                connect_kwargs["key_filename"] = str(self.ssh_key)
        elif self.ssh_password:
            connect_kwargs["password"] = self.ssh_password
        else:
            connect_kwargs["look_for_keys"] = True
            connect_kwargs["allow_agent"] = True
        client.connect(**connect_kwargs)
        return client

    def _ssh_run(self, client: paramiko.SSHClient, cmd: str, check: bool = False) -> tuple[int, str, str]:
        """Run a sudo command on the remote mgmt node."""
        full_cmd = f"sudo bash -c {shlex.quote(cmd)}"
        stdin, stdout, stderr = client.exec_command(full_cmd, timeout=120)
        stdin.flush()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        if check and rc != 0:
            raise RuntimeError(
                f"Remote command failed (exit {rc}): {cmd!r}\nstdout: {out}\nstderr: {err}"
            )
        return rc, out, err

    def _setup_disk_storage(self) -> None:
        """
        1. Format disk_device as ext4 on the mgmt node (skip if already formatted).
        2. Mount at storage_mount_path on the mgmt node.
        3. Persist the mount in /etc/fstab on the mgmt node.
        4. Deploy local-path-provisioner pointing at the mount path.
        """
        log.info("[registry] Setting up disk storage: %s → %s",
                 self.disk_device, self.storage_mount_path)

        node_ip = self._get_mgmt_node_ip()
        log.info("[registry] Connecting to mgmt node %s via SSH to set up disk...", node_ip)
        client = self._ssh_connect(node_ip)
        try:
            rc, _, _ = self._ssh_run(client, f"mountpoint -q {self.storage_mount_path}")
            if rc == 0:
                log.info("[registry] %s already mounted at %s", self.disk_device, self.storage_mount_path)
            else:
                self._format_and_mount(client)

            # Ensure permissions are open for the kubelet to write
            self._ssh_run(client, f"chmod 777 {self.storage_mount_path}", check=True)
        finally:
            client.close()

        self._deploy_local_path_provisioner()

    def _format_and_mount(self, client: paramiko.SSHClient) -> None:
        rc, out, _ = self._ssh_run(client, f"blkid {self.disk_device}")
        if rc != 0 or not out.strip():
            log.info("[registry] No filesystem on %s — formatting as ext4...", self.disk_device)
            self._ssh_run(client, f"mkfs.ext4 -F {self.disk_device}", check=True)
            log.info("[registry] Formatted %s", self.disk_device)
        else:
            log.info("[registry] Existing filesystem detected on %s — skipping format", self.disk_device)

        self._ssh_run(client, f"mkdir -p {self.storage_mount_path}", check=True)
        self._ssh_run(client, f"mount {self.disk_device} {self.storage_mount_path}", check=True)
        log.info("[registry] Mounted %s at %s", self.disk_device, self.storage_mount_path)

        self._add_fstab_entry(client)

    def _add_fstab_entry(self, client: paramiko.SSHClient) -> None:
        rc, _, _ = self._ssh_run(client, f"grep -qF {shlex.quote(self.disk_device)} /etc/fstab")
        if rc == 0:
            log.debug("[registry] /etc/fstab entry for %s already present", self.disk_device)
            return
        entry = f"{self.disk_device}  {self.storage_mount_path}  ext4  defaults  0  2"
        self._ssh_run(client, f"echo {shlex.quote(entry)} >> /etc/fstab", check=True)
        log.info("[registry] Added %s to /etc/fstab", self.disk_device)

    def _deploy_local_path_provisioner(self) -> None:
        """
        Deploy Rancher local-path-provisioner and point its ConfigMap at
        storage_mount_path so all "local-path" PVCs land on the disk.
        """
        log.info("[registry] Deploying local-path-provisioner...")
        self._kubectl("apply", "-f", LOCAL_PATH_PROVISIONER_URL)

        # Wait for the provisioner deployment to be ready
        result = self._kubectl(
            "rollout", "status", "deployment/local-path-provisioner",
            "-n", "local-path-storage",
            "--timeout=300s",
            check=False,
        )
        if result.returncode != 0:
            log.warning(
                "[registry] local-path-provisioner rollout did not complete (is the node Ready?). "
                "Harbor PVCs may fail to bind if the node remains NotReady."
            )

        # Patch the ConfigMap to use our mount path
        config_patch = {
            "data": {
                "config.json": json.dumps({
                    "nodePathMap": [
                        {
                            "node": "DEFAULT_PATH_FOR_NON_LISTED_NODES",
                            "paths": [self.storage_mount_path],
                        }
                    ]
                })
            }
        }
        self._kubectl(
            "patch", "configmap", "local-path-config",
            "-n", "local-path-storage",
            "--type=merge",
            f"--patch={json.dumps(config_patch)}",
        )
        log.info("[registry] local-path-provisioner ready, using %s", self.storage_mount_path)

    # ------------------------------------------------------------------
    # PVC cleanup
    # ------------------------------------------------------------------

    def _cleanup_orphaned_pvcs(self) -> None:
        """
        Delete PVCs left behind by a previous failed install.
        Only runs when the Helm release does not exist (i.e. no running Harbor),
        so it is safe to remove them.
        """
        status = self._helm("status", "harbor", "-n", self.namespace, check=False)
        if status.returncode == 0:
            # Release exists — Harbor is (or was) running; keep PVCs.
            return

        result = subprocess.run(
            ["kubectl", "--kubeconfig", self.mgmt_kubeconfig,
             "get", "pvc", "-n", self.namespace, "--no-headers", "-o", "name"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            log.info("[registry] Removing orphaned PVCs from previous failed install...")
            self._kubectl("delete", "pvc", "--all", "-n", self.namespace, check=False)

    # ------------------------------------------------------------------
    # Helm install / upgrade
    # ------------------------------------------------------------------

    def _ensure_namespace(self) -> None:
        log.info("[registry] Ensuring namespace '%s' exists...", self.namespace)
        self._kubectl("create", "namespace", self.namespace, check=False)

    def _helm_add_repo(self) -> None:
        log.info("[registry] Adding Harbor Helm repo...")
        self._helm("repo", "add", HARBOR_REPO_NAME, HARBOR_REPO_URL, check=False)
        self._helm("repo", "update")

    def _helm_install_or_upgrade(self) -> None:
        log.info("[registry] Installing/upgrading Harbor chart...")

        if self.local_chart_dir and Path(self.local_chart_dir).is_dir():
            chart = str(self.local_chart_dir)
            log.info("[registry] Using local chart: %s", chart)
        else:
            chart = HARBOR_CHART
            log.info("[registry] Using remote chart: %s", chart)

        runtime_values = self._build_runtime_values()
        values_yaml = yaml.dump(runtime_values)

        cmd = [
            "upgrade", "--install", "harbor", chart,
            "--namespace", self.namespace,
            "--cleanup-on-fail",
        ]

        # Static base values file first (lower priority)
        if self.values_path and Path(self.values_path).is_file():
            cmd += ["--values", str(self.values_path)]

        # Runtime values last — stdin wins over the base file
        cmd += ["--values", "-"]

        self._helm(*cmd, input=values_yaml)

    def _build_runtime_values(self) -> dict:
        """Build the runtime Helm values dict with secrets/hostname/storage injected."""
        pvc = {
            "registry": {"size": self.storage_size},
            "jobservice": {"jobLog": {"size": "1Gi"}},
            "database": {"size": "1Gi"},
            "redis": {"size": "1Gi"},
        }

        # Wire every PVC to the configured StorageClass when set.
        # jobservice is nested under jobLog in the Harbor chart schema.
        if self.storage_class:
            for key, section in pvc.items():
                if key == "jobservice":
                    section["jobLog"]["storageClass"] = self.storage_class
                else:
                    section["storageClass"] = self.storage_class

        # Use harbor_node_ip as the cert commonName and externalURL when set,
        # so the auto-generated cert's SAN matches the actual NodePort IP.
        effective_host = self.harbor_node_ip or self.harbor_hostname

        return {
            "expose": {
                "type": "nodePort",
                "nodePort": {
                    "ports": {
                        "https": {"port": 443, "nodePort": 30003},
                        "http": {"port": 80, "nodePort": 30002},
                    }
                },
                "tls": {
                    "enabled": True,
                    "certSource": "auto",
                    "auto": {"commonName": effective_host},
                },
            },
            "externalURL": f"https://{effective_host}:30003",
            "harborAdminPassword": self.admin_password,
            "trivy": {"enabled": False},
            "notary": {"enabled": False},
            "persistence": {"persistentVolumeClaim": pvc},
            # local-path-provisioner creates hostPath dirs owned by root.
            # Set fsGroup=999 (postgres gid) so kubelet chowns the volume on
            # mount. runAsUser=999 matches the postgres user inside the image.
            "database": {
                "internal": {
                    "podSecurityContext": {
                        "runAsUser": 999,
                        "runAsGroup": 999,
                        "fsGroup": 999,
                    }
                }
            },
            # Disable RDB snapshots so Redis never hits stop-writes-on-bgsave-error.
            # Without this, any RDB save failure (common on local-path storage with
            # restricted permissions) blocks ALL Redis writes, causing Harbor Core
            # to return HTTP 500 on every blob upload.
            # `redis.internal.configuration` is appended to redis.conf in the
            # Harbor chart ConfigMap, so it persists across pod restarts.
            "redis": {
                "internal": {
                    "configuration": 'save ""\nstop-writes-on-bgsave-error no\n',
                }
            },
        }

    # ------------------------------------------------------------------
    # Post-install steps
    # ------------------------------------------------------------------

    def _wait_ready(self) -> None:
        """
        Wait for Harbor by rolling out each deployment and statefulset individually.
        This avoids the race where `kubectl wait pods --all` captures terminating pods
        from a previous revision that disappear mid-wait.
        """
        log.info("[registry] Waiting for Harbor rollout to complete (timeout 10m)...")
        for kind in ("deployment", "statefulset"):
            result = subprocess.run(
                ["kubectl", "--kubeconfig", self.mgmt_kubeconfig,
                 "get", kind, "-n", self.namespace, "-o", "name"],
                capture_output=True, text=True, check=False,
            )
            for resource in result.stdout.strip().splitlines():
                log.info("[registry] Waiting for %s...", resource)
                self._kubectl(
                    "rollout", "status", resource,
                    "-n", self.namespace,
                    "--timeout=600s",
                )
        log.info("[registry] Harbor is ready")

    def _fix_storage_permissions(self) -> None:
        """
        Chown Harbor PVC hostPath directories to the Harbor process uid (10000).

        local-path-provisioner creates hostPath directories as root:root 755.
        The Kubernetes fsGroup + OnRootMismatch policy does not reliably apply
        recursive chown to HostPath-backed PVCs, so Harbor's registry and
        jobservice containers (uid/gid 10000) get "permission denied" when they
        try to create their first subdirectory under /storage.

        Fix: run a short-lived privileged Job that directly chowns the host
        paths via hostPath volume mounts.
        """
        log.info("[registry] Fixing storage permissions for Harbor PVCs...")

        pvc_targets = [
            ("harbor-registry", 10000),
            ("harbor-jobservice", 10000),
        ]

        volumes, mounts, cmds = [], [], []
        for i, (pvc_name, uid) in enumerate(pvc_targets):
            host_path = self._get_pvc_hostpath(pvc_name)
            if not host_path:
                log.warning("[registry] Could not resolve hostPath for PVC %s — skipping", pvc_name)
                continue
            vol_name = f"vol{i}"
            mnt = f"/fix{i}"
            volumes.append({"name": vol_name, "hostPath": {"path": host_path, "type": "Directory"}})
            mounts.append({"name": vol_name, "mountPath": mnt})
            cmds.append(f"chown -R {uid}:{uid} {mnt} && chmod -R u+rwX {mnt} && echo '{pvc_name} OK'")

        if not volumes:
            log.warning("[registry] No PVC hostPaths resolved — skipping permission fix")
            return

        job_name = "harbor-fix-perms"
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job_name, "namespace": self.namespace},
            "spec": {
                "ttlSecondsAfterFinished": 30,
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "tolerations": [{
                            "key": "node-role.kubernetes.io/control-plane",
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        }],
                        "containers": [{
                            "name": "fix-perms",
                            "image": "busybox:latest",
                            "command": ["sh", "-c", " && ".join(cmds)],
                            "securityContext": {"runAsUser": 0},
                            "volumeMounts": mounts,
                        }],
                        "volumes": volumes,
                    }
                },
            },
        }

        # Delete any pre-existing job (idempotent re-runs)
        subprocess.run(
            ["kubectl", "--kubeconfig", self.mgmt_kubeconfig,
             "delete", "job", job_name, "-n", self.namespace],
            capture_output=True, check=False,
        )
        self._kubectl_input("apply", "-f", "-", input=yaml.dump(job))
        subprocess.run(
            ["kubectl", "--kubeconfig", self.mgmt_kubeconfig,
             "wait", "-n", self.namespace, f"job/{job_name}",
             "--for=condition=Complete", "--timeout=120s"],
            check=True,
        )
        log.info("[registry] Storage permissions fixed")

    def _get_pvc_hostpath(self, pvc_name: str) -> str | None:
        """Return the hostPath of the PV backing a PVC, or None if not resolvable."""
        result = subprocess.run(
            ["kubectl", "--kubeconfig", self.mgmt_kubeconfig,
             "get", "pvc", pvc_name, "-n", self.namespace,
             "-o", "jsonpath={.spec.volumeName}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        pv_name = result.stdout.strip()
        result = subprocess.run(
            ["kubectl", "--kubeconfig", self.mgmt_kubeconfig,
             "get", "pv", pv_name,
             "-o", "jsonpath={.spec.hostPath.path}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip()

    def _configure_redis(self) -> None:
        """
        Disable RDB snapshots and stop-writes-on-bgsave-error in Harbor's Redis.

        Redis blocks ALL writes when an RDB snapshot fails (e.g. the /data dir
        has permission issues on local-path storage).  This causes Harbor Core
        to return HTTP 500 on every blob upload and silently reject logins.

        This live fix is belt-and-suspenders: the permanent fix is the
        `redis.internal.configuration` Helm value which bakes these settings
        into the Redis ConfigMap at install/upgrade time.
        """
        log.info("[registry] Configuring Redis: disabling RDB snapshots...")
        for key, val in [("save", ""), ("stop-writes-on-bgsave-error", "no")]:
            result = subprocess.run(
                [
                    "kubectl", "--kubeconfig", self.mgmt_kubeconfig,
                    "exec", "-n", self.namespace, "harbor-redis-0",
                    "--", "redis-cli", "CONFIG", "SET", key, val,
                ],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                log.warning(
                    "[registry] Redis CONFIG SET %s failed (non-fatal): %s",
                    key, result.stderr.strip(),
                )
        log.info("[registry] Redis configured")

    def _configure_containerd_on_mgmt_node(self) -> None:
        """
        Configure containerd on the mgmt node to skip TLS verification for Harbor.

        Writes /etc/containerd/certs.d/{registry}/hosts.toml with skip_verify=true
        and ensures containerd's config.toml references certs.d, then restarts
        containerd so the change takes effect immediately.

        Skipped silently when no SSH credentials are available.
        """
        if not self.ssh_key and not self.ssh_password:
            log.warning(
                "[registry] No SSH credentials provided — skipping containerd "
                "configuration on mgmt node. Pulls from Harbor may fail with TLS errors."
            )
            return

        node_ip = self._get_mgmt_node_ip()
        registry = f"{node_ip}:30003"
        log.info(
            "[registry] Configuring containerd on mgmt node %s to trust %s...",
            node_ip, registry,
        )

        hosts_toml = (
            f'server = "https://{registry}"\n\n'
            f'[host."https://{registry}"]\n'
            f'  capabilities = ["pull", "resolve"]\n'
            f'  skip_verify = true\n'
        )
        hosts_toml_b64 = base64.b64encode(hosts_toml.encode()).decode()

        client = self._ssh_connect(node_ip)
        try:
            sftp = client.open_sftp()
            # Write hosts.toml via SFTP to /tmp then move with sudo
            tmp = f"/tmp/harbor-hosts-{node_ip.replace('.', '_')}.toml"
            with sftp.open(tmp, "w") as f:
                f.write(hosts_toml)
            sftp.close()

            certs_dir = f"/etc/containerd/certs.d/{registry}"
            self._ssh_run(client, f"mkdir -p {certs_dir}", check=True)
            self._ssh_run(client, f"mv {tmp} {certs_dir}/hosts.toml", check=True)

            # Patch containerd config.toml to enable config_path if not already set
            patch_cmd = (
                "CONFIG=/etc/containerd/config.toml\n"
                "CERTS_PATH=/etc/containerd/certs.d\n"
                "if [ ! -f \"$CONFIG\" ]; then exit 0; fi\n"
                "if grep -q 'config_path' \"$CONFIG\"; then\n"
                "  sed -i 's|config_path = .*|config_path = \"'\"$CERTS_PATH\"'\"|' \"$CONFIG\"\n"
                "elif grep -q '\\[plugins\\.\"io\\.containerd\\.grpc\\.v1\\.cri\"\\]\\.registry' \"$CONFIG\"; then\n"
                "  sed -i '/registry\\]/a\\  config_path = \"'\"$CERTS_PATH\"'\"' \"$CONFIG\"\n"
                "else\n"
                "  printf '\\n[plugins.\"io.containerd.grpc.v1.cri\".registry]\\n  config_path = \"%s\"\\n' \"$CERTS_PATH\" >> \"$CONFIG\"\n"
                "fi"
            )
            self._ssh_run(client, patch_cmd, check=True)
            self._ssh_run(client, "systemctl restart containerd", check=True)
            log.info(
                "[registry] containerd on mgmt node configured and restarted for %s",
                registry,
            )
        except Exception as exc:
            log.warning(
                "[registry] Failed to configure containerd on mgmt node: %s "
                "(Harbor pulls may fail with TLS errors — run manually if needed)",
                exc,
            )
        finally:
            client.close()

    def configure_cluster_registry_trust(self, cluster_kubeconfig: str) -> None:
        """
        Configure containerd on every infra-cluster node to trust Harbor via a
        privileged DaemonSet that writes /etc/containerd/certs.d/{registry}/hosts.toml
        with skip_verify=true and restarts containerd on each node.

        Waits for all nodes to be configured then deletes the DaemonSet.

        Call this after deploying Harbor, passing the infra cluster's kubeconfig.
        """
        registry = self._access_url or f"{self._get_mgmt_node_ip()}:30003"
        log.info(
            "[registry] Configuring infra cluster nodes to trust Harbor at %s...",
            registry,
        )

        hosts_toml = (
            f'server = "https://{registry}"\n\n'
            f'[host."https://{registry}"]\n'
            f'  capabilities = ["pull", "resolve"]\n'
            f'  skip_verify = true\n'
        )
        hosts_toml_b64 = base64.b64encode(hosts_toml.encode()).decode()

        # Shell script run inside each DaemonSet pod (host filesystem at /host).
        # Two-pronged approach so it works on both containerd 1.x and 2.x:
        #   1. hosts.toml (new-style, needs config_path)
        #   2. insecure_skip_verify in config.toml (old-style, no config_path needed)
        configure_sh = "\n".join([
            "#!/bin/sh",
            "set -e",
            f'REGISTRY="{registry}"',
            # --- new-style: certs.d/hosts.toml ---
            "CERTS_DIR=/host/etc/containerd/certs.d",
            'mkdir -p "$CERTS_DIR/$REGISTRY"',
            f'echo "{hosts_toml_b64}" | base64 -d > "$CERTS_DIR/$REGISTRY/hosts.toml"',
            'echo "Wrote hosts.toml for $REGISTRY"',
            # --- new-style: ensure config_path is set ---
            "CONFIG=/host/etc/containerd/config.toml",
            'if [ -f "$CONFIG" ]; then',
            '  if grep -q "config_path" "$CONFIG"; then',
            '    sed -i \'s|config_path = .*|config_path = "/etc/containerd/certs.d"|\' "$CONFIG"',
            '  else',
            '    printf \'\\n[plugins."io.containerd.grpc.v1.cri".registry]\\n  config_path = "/etc/containerd/certs.d"\\n\' >> "$CONFIG"',
            '  fi',
            # --- old-style fallback: insecure_skip_verify (works without config_path) ---
            f'  if ! grep -q "insecure_skip_verify" "$CONFIG"; then',
            f'    printf \'\\n[plugins."io.containerd.grpc.v1.cri".registry.configs."%s".tls]\\n  insecure_skip_verify = true\\n\' "$REGISTRY" >> "$CONFIG"',
            '  fi',
            "fi",
            # Restart containerd on the host via nsenter into the host mount namespace
            "nsenter -m/proc/1/ns/mnt -- systemctl restart containerd",
            f'echo "Node configured for $REGISTRY"',
            "sleep infinity",
        ])

        ds_name = "harbor-registry-trust"
        daemonset = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": ds_name, "namespace": "kube-system"},
            "spec": {
                "selector": {"matchLabels": {"app": ds_name}},
                "template": {
                    "metadata": {"labels": {"app": ds_name}},
                    "spec": {
                        "hostPID": True,
                        "tolerations": [{"operator": "Exists"}],
                        "containers": [{
                            "name": "configure",
                            "image": "busybox:latest",
                            "command": ["sh", "-c", configure_sh],
                            "securityContext": {"privileged": True},
                            "volumeMounts": [
                                {"name": "host-root", "mountPath": "/host"},
                            ],
                        }],
                        "volumes": [
                            {"name": "host-root", "hostPath": {"path": "/"}},
                        ],
                    },
                },
            },
        }

        # Delete any leftover from a previous run
        subprocess.run(
            ["kubectl", "--kubeconfig", cluster_kubeconfig,
             "delete", "daemonset", ds_name, "-n", "kube-system"],
            capture_output=True, check=False,
        )

        result = subprocess.run(
            ["kubectl", "--kubeconfig", cluster_kubeconfig, "apply", "-f", "-"],
            input=yaml.dump(daemonset), text=True, check=False, capture_output=True,
        )
        if result.returncode != 0:
            log.warning(
                "[registry] Failed to deploy containerd-config DaemonSet: %s",
                result.stderr.strip(),
            )
            return

        log.info("[registry] Waiting for all nodes to be configured (timeout 3m)...")
        deadline = time.time() + 180
        while time.time() < deadline:
            r = subprocess.run(
                ["kubectl", "--kubeconfig", cluster_kubeconfig,
                 "get", "daemonset", ds_name, "-n", "kube-system",
                 "-o", "jsonpath={.status.numberReady}/{.status.desiredNumberScheduled}"],
                capture_output=True, text=True, check=False,
            )
            status = r.stdout.strip()
            if "/" in status:
                ready, desired = status.split("/", 1)
                if ready == desired and desired != "0":
                    log.info("[registry] All %s node(s) configured", desired)
                    break
            time.sleep(5)
        else:
            log.warning(
                "[registry] Timed out waiting for containerd config DaemonSet. "
                "Some nodes may not be configured yet."
            )

        # Clean up
        subprocess.run(
            ["kubectl", "--kubeconfig", cluster_kubeconfig,
             "delete", "daemonset", ds_name, "-n", "kube-system"],
            capture_output=True, check=False,
        )
        log.info("[registry] Containerd configured on all infra cluster nodes for %s", registry)

    def _ensure_harbor_project(self) -> None:
        access = self._access_url or self.harbor_hostname
        log.info("[registry] Ensuring Harbor project '%s' exists...", self.harbor_project)
        url = f"https://{access}/api/v2.0/projects"
        try:
            resp = requests.post(
                url,
                json={"project_name": self.harbor_project, "public": False},
                auth=("admin", self.admin_password),
                verify=False,
                timeout=30,
            )
            if resp.status_code == 201:
                log.info("[registry] Harbor project '%s' created", self.harbor_project)
            elif resp.status_code == 409:
                log.info("[registry] Harbor project '%s' already exists", self.harbor_project)
            elif resp.status_code == 401:
                log.warning(
                    "[registry] Harbor API returned 401 Unauthorized at %s. "
                    "The admin_password in config may not match Harbor's stored password "
                    "(Harbor retains the password from its first install). "
                    "Project may need to be created manually.",
                    url,
                )
            else:
                log.warning(
                    "[registry] Harbor API returned HTTP %d at %s. "
                    "Project '%s' may need to be created manually via the Harbor UI.",
                    resp.status_code, url, self.harbor_project,
                )
        except requests.exceptions.RequestException as exc:
            log.warning(
                "[registry] Could not reach Harbor API at %s: %s. "
                "Project may need to be created manually.",
                url, exc,
            )
