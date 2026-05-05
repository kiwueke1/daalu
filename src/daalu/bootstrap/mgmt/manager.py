# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/mgmt/manager.py

from __future__ import annotations

import logging
from pathlib import Path

import paramiko

from daalu.bootstrap.mgmt.k8s_installer import K8sInstaller
from daalu.bootstrap.mgmt.metal3_installer import Metal3Installer
from daalu.bootstrap.mgmt.models import MgmtClusterConfig
from daalu.bootstrap.registry.manager import RegistryManager
from daalu.config.models import DaaluConfig
from daalu.utils.ssh_runner import SSHRunner

log = logging.getLogger("daalu")


class MgmtClusterManager:
    """
    Orchestrates the full management cluster bootstrap:

      1. SSH into a fresh Ubuntu node
      2. Install Kubernetes (kubeadm) + Cilium CNI
      3. Save kubeconfig locally
      4. Install the bare-metal provisioning stack, chosen by mgmt_cluster.provider:
           tinkerbell  (default) — Tinkerbell stack + CAPT
           metal3                — Ironic + Baremetal Operator + CAPM3
           proxmox               — Cluster API Provider for Proxmox (CAPO)
      5. Deploy Harbor registry (optional, reuses RegistryManager)

    Returns the local kubeconfig path so the user can point their tools at
    the new cluster immediately.
    """

    def __init__(self, cfg: DaaluConfig, workspace_root: Path):
        self._cfg = cfg
        self._workspace_root = workspace_root

    def deploy(self) -> tuple[str, str | None]:
        """
        Run the full mgmt cluster bootstrap.
        Returns (kubeconfig_path, harbor_url_or_none).
        """
        mgmt_cfg = self._cfg.mgmt_cluster
        if not mgmt_cfg:
            raise ValueError(
                "mgmt_cluster block is required in cluster.yaml for mgmt cluster deployment. "
                "See the mgmt_cluster section in cloud-config/secrets.yaml.example."
            )

        kubeconfig_path = str(
            Path(mgmt_cfg.kubeconfig_output_path).expanduser().resolve()
        )

        # ------------------------------------------------------------------
        # 1. SSH connection to the fresh Ubuntu node
        # ------------------------------------------------------------------
        log.info("[mgmt] Connecting to %s as %s...", mgmt_cfg.host, mgmt_cfg.ssh_username)
        client = self._ssh_connect(mgmt_cfg)
        ssh = SSHRunner(client)

        harbor_url: str | None = None
        registry_mgr: RegistryManager | None = None
        try:
            # ------------------------------------------------------------------
            # 2. Install Kubernetes + Cilium
            # ------------------------------------------------------------------
            k8s = K8sInstaller(ssh, mgmt_cfg)
            kubeconfig_text = k8s.install()

            # ------------------------------------------------------------------
            # 3. Save kubeconfig locally
            # ------------------------------------------------------------------
            log.info("[mgmt] Writing kubeconfig to %s", kubeconfig_path)
            kc_path = Path(kubeconfig_path)
            kc_path.parent.mkdir(parents=True, exist_ok=True)
            kc_path.write_text(kubeconfig_text)
            kc_path.chmod(0o600)

            # Also update ~/.kube/config so plain `kubectl` works immediately.
            # Each fresh kubeadm init generates new CA certs, so the old
            # ~/.kube/config becomes invalid anyway.
            default_kc = Path.home() / ".kube" / "config"
            if kc_path.resolve() != default_kc.resolve():
                default_kc.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(kc_path, default_kc)
                default_kc.chmod(0o600)
                log.info("[mgmt] Updated ~/.kube/config with new cluster credentials")

            # ------------------------------------------------------------------
            # 4. Install Cilium (runs locally via helm pointing at remote cluster)
            # ------------------------------------------------------------------
            k8s.install_cilium(kubeconfig_path)

            # ------------------------------------------------------------------
            # 5. Install bare-metal provisioning stack
            # ------------------------------------------------------------------
            from daalu.bootstrap.mgmt.models import BaremetalProvider

            if mgmt_cfg.skip_provisioning_stack:
                log.info(
                    "[mgmt] skip_provisioning_stack=True — skipping bare-metal "
                    "provisioning stack installation (cert-manager, CAPI, provider stack)."
                )
            else:
                provider = mgmt_cfg.provider
                log.info("[mgmt] Installing bare-metal provisioning stack (provider: %s)...", provider.value)

                if provider == BaremetalProvider.metal3:
                    Metal3Installer(kubeconfig_path, mgmt_cfg).install()

                elif provider == BaremetalProvider.tinkerbell:
                    from daalu.bootstrap.mgmt.tinkerbell_installer import TinkerbellInstaller
                    TinkerbellInstaller(kubeconfig_path, mgmt_cfg, self._workspace_root).install()

                elif provider == BaremetalProvider.proxmox:
                    from daalu.bootstrap.mgmt.proxmox_installer import ProxmoxInstaller
                    ProxmoxInstaller(kubeconfig_path, mgmt_cfg).install()

                else:
                    raise ValueError(f"Unknown bare-metal provider: {provider!r}")

            # ------------------------------------------------------------------
            # 6. Deploy Harbor (optional)
            # ------------------------------------------------------------------
            if mgmt_cfg.install_harbor and self._cfg.registry:
                registry_cfg = self._cfg.registry

                # Auto-detect a free disk if disk_device is not configured
                if not registry_cfg.disk_device:
                    free_disk = self._detect_free_disk(ssh)
                    if free_disk:
                        log.info("[mgmt] Auto-detected free disk for Harbor: %s", free_disk)
                        registry_cfg = registry_cfg.model_copy(
                            update={"disk_device": free_disk}
                        )
                    else:
                        log.warning(
                            "[mgmt] No free disk detected — Harbor PVCs will use the OS disk. "
                            "Set registry.disk_device in secrets.yaml to use a dedicated disk."
                        )

                log.info("[mgmt] Deploying Harbor registry...")
                registry_mgr = RegistryManager(
                    registry_cfg=registry_cfg,
                    workspace_root=self._workspace_root,
                    secrets_path=self._workspace_root / "cloud-config" / "secrets.yaml",
                )
                registry_mgr.deploy_harbor(
                    mgmt_kubeconfig=kubeconfig_path,
                    ssh_key=mgmt_cfg.ssh_key,
                    ssh_username=mgmt_cfg.ssh_username,
                    ssh_password=mgmt_cfg.ssh_password,
                )
                registry_mgr.mirror_images()
                harbor_url = registry_mgr.harbor_registry_url()

                # Pre-create Harbor projects referenced by the Temporal images
                # so the operator can `docker push` between pass 1 (Harbor only)
                # and pass 2 (Temporal install) without a manual curl. Runs even
                # when temporal.enabled=False — the projects are still needed
                # the moment the operator builds and pushes the images.
                if mgmt_cfg.temporal:
                    from daalu.bootstrap.mgmt.temporal_installer import TemporalInstaller
                    extras = {
                        TemporalInstaller._project_from_image(img)
                        for img in (
                            mgmt_cfg.temporal.worker_image,
                            mgmt_cfg.temporal.console_image,
                        )
                    } - {None, registry_mgr.harbor_project()}
                    for project in sorted(p for p in extras if p):
                        try:
                            registry_mgr.ensure_project(project)
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "[mgmt] Could not pre-create Harbor project "
                                "'%s' (needed for Temporal images): %s",
                                project, exc,
                            )
            elif mgmt_cfg.install_harbor and not self._cfg.registry:
                log.warning(
                    "[mgmt] install_harbor=true but no 'registry:' block found in config. "
                    "Add a registry block to cluster.yaml to enable Harbor deployment."
                )

            # ------------------------------------------------------------------
            # 7. Install Temporal control plane (server + daalu worker + UI)
            # ------------------------------------------------------------------
            # Runs after Harbor so the worker image is fetchable from the local
            # registry. Configurable via mgmt_cluster.temporal.* in cluster.yaml.
            if mgmt_cfg.temporal and mgmt_cfg.temporal.enabled:
                from daalu.bootstrap.mgmt.temporal_installer import TemporalInstaller
                try:
                    TemporalInstaller(
                        kubeconfig_path=kubeconfig_path,
                        cfg=mgmt_cfg,
                        workspace_root=self._workspace_root,
                        registry_mgr=registry_mgr,
                    ).install()
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "[mgmt] Temporal install failed (non-fatal — daalu CLI "
                        "still works): %s",
                        exc,
                    )

        finally:
            client.close()

        log.info("[mgmt] Management cluster bootstrap complete.")
        return kubeconfig_path, harbor_url

    # ------------------------------------------------------------------
    # Disk auto-detection
    # ------------------------------------------------------------------

    def _detect_free_disk(self, ssh: SSHRunner) -> str | None:
        """
        Find the first disk on the remote node that has no partitions and is
        not mounted anywhere — i.e. safe to format for Harbor storage.

        Uses lsblk: a disk is considered free when lsblk lists exactly one
        line for it (the disk itself, no children/partitions).
        """
        rc, out, _ = ssh.run(
            "lsblk -dpno NAME,TYPE | awk '$2==\"disk\"{print $1}'",
            sudo=False,
        )
        if rc != 0 or not out.strip():
            return None

        for disk in out.strip().splitlines():
            disk = disk.strip()
            if not disk:
                continue
            # Count lines: disk itself = 1, any partition adds more
            rc2, out2, _ = ssh.run(
                f"lsblk -no NAME {disk} | wc -l",
                sudo=False,
            )
            if rc2 == 0 and out2.strip() == "1":
                return disk

        return None

    # ------------------------------------------------------------------
    # SSH helpers
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
            # Try common key types
            pkey = None
            for key_class in (
                paramiko.Ed25519Key,
                paramiko.RSAKey,
                paramiko.ECDSAKey,
            ):
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
            # Fall back to ssh-agent / default key files
            connect_kwargs["look_for_keys"] = True
            connect_kwargs["allow_agent"] = True

        client.connect(**connect_kwargs)
        log.info("[mgmt] SSH connected to %s", cfg.host)
        return client
