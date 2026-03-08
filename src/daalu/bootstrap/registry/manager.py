# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/registry/manager.py

from __future__ import annotations

import logging
from pathlib import Path

from daalu.bootstrap.registry.harbor_deployer import HarborDeployer
from daalu.bootstrap.registry.image_builder import ImageBuilder
from daalu.bootstrap.registry.image_extractor import ImageExtractor
from daalu.bootstrap.registry.image_mirror import ImageMirror

log = logging.getLogger("daalu")


class RegistryManager:
    """
    Top-level orchestrator for the Harbor container registry feature.

    Responsibilities:
      1. Deploy Harbor to the mgmt cluster (HarborDeployer).
      2. Extract all images referenced in assets/ values files (ImageExtractor).
      3. Mirror those images from their source registries into Harbor (ImageMirror).
    """

    def __init__(self, *, registry_cfg, workspace_root: Path, secrets_path: Path):
        self.registry_cfg = registry_cfg
        self.workspace_root = workspace_root
        self.secrets_path = secrets_path

    @property
    def admin_password(self) -> str:
        # Populated from secrets.yaml via the `registry.admin_password` key,
        # which is deep-merged into RegistryConfig by the config loader.
        pw = getattr(self.registry_cfg, "admin_password", None)
        if not pw:
            raise ValueError(
                "Harbor admin password not set. "
                "Add 'registry:\\n  admin_password: \"<password>\"' to cloud-config/secrets.yaml."
            )
        return pw

    def deploy_harbor(
        self,
        mgmt_kubeconfig: str | None = None,
        ssh_key: str | None = None,
        ssh_username: str = "ubuntu",
        ssh_password: str | None = None,
        cluster_kubeconfig: str | None = None,
    ) -> None:
        """Deploy Harbor to the mgmt cluster.

        If cluster_kubeconfig is provided, also configures containerd on every
        infra-cluster node to trust Harbor's self-signed certificate.
        """
        effective_kubeconfig = (
            mgmt_kubeconfig
            or getattr(self.registry_cfg, "mgmt_kubeconfig", None)
        )
        if not effective_kubeconfig:
            raise ValueError(
                "mgmt_kubeconfig is required for Harbor deployment. "
                "Pass --mgmt-kubeconfig or set registry.mgmt_kubeconfig in cluster.yaml."
            )

        harbor_assets = self.workspace_root / "assets" / "harbor"
        values_path = harbor_assets / "values.yaml"
        local_chart_dir = harbor_assets / "charts" / "harbor"

        self._deployer = HarborDeployer(
            mgmt_kubeconfig=effective_kubeconfig,
            harbor_hostname=self.registry_cfg.harbor_hostname,
            admin_password=self.admin_password,
            namespace=self.registry_cfg.harbor_namespace,
            storage_size=self.registry_cfg.harbor_storage_size,
            values_path=values_path,
            harbor_project=self.registry_cfg.project,
            local_chart_dir=local_chart_dir if local_chart_dir.is_dir() else None,
            disk_device=self.registry_cfg.disk_device,
            storage_mount_path=self.registry_cfg.storage_mount_path,
            storage_class=self.registry_cfg.storage_class,
            ssh_key=ssh_key,
            ssh_username=ssh_username,
            ssh_password=ssh_password,
            harbor_node_ip=getattr(self.registry_cfg, "harbor_node_ip", None),
        )
        self._deployer.deploy()
        self._harbor_access_url = self._deployer.get_registry_url()

        if cluster_kubeconfig:
            self._deployer.configure_cluster_registry_trust(cluster_kubeconfig)

    def mirror_images(self) -> None:
        """Build source images, then mirror the rest from upstream into Harbor."""
        assets_dir = self.workspace_root / "assets"
        harbor_url = getattr(self, "_harbor_access_url", None) or self.registry_cfg.harbor_hostname

        # Step 1: build any images that require compilation from source
        builder = ImageBuilder(
            assets_dir=assets_dir,
            harbor_hostname=harbor_url,
            harbor_project=self.registry_cfg.project,
            admin_password=self.admin_password,
        )
        build_report = builder.build_all(skip_existing=True)
        if build_report.failed:
            log.warning("[registry] %d image(s) failed to build: %s", len(build_report.failed), build_report.failed)

        # Step 2: extract all image refs from values files, exclude source images
        # that were just built (they're private upstream and don't need mirroring)
        build_source_images = builder.source_images()
        images = [
            img for img in ImageExtractor(assets_dir).extract_all()
            if img not in build_source_images
        ]
        log.info("[registry] Extracted %d unique images from assets/ (%d excluded as build-from-source)",
                 len(images), len(build_source_images))

        images_file = self.workspace_root / "images.txt"
        images_file.write_text("\n".join(images) + "\n")
        log.info("[registry] Image list written to %s", images_file)

        mirror = ImageMirror(
            harbor_hostname=getattr(self, "_harbor_access_url", None) or self.registry_cfg.harbor_hostname,
            harbor_project=self.registry_cfg.project,
            admin_password=self.admin_password,
            insecure=True,
            skip_existing=True,
            docker_username=getattr(self.registry_cfg, "docker_username", None) or None,
            docker_token=getattr(self.registry_cfg, "docker_token", None) or None,
        )
        log.info("[registry] Images to mirror:")
        for img in images:
            log.info("[registry]   %s", img)
        report = mirror.mirror_all(images)

        if report.failed:
            log.warning(
                "[registry] %d image(s) failed to mirror: %s",
                report.failed, report.failed_images,
            )

    def configure_cluster_registry_trust(self, cluster_kubeconfig: str) -> None:
        """Configure containerd on infra cluster nodes to trust Harbor's cert."""
        deployer = getattr(self, "_deployer", None)
        if deployer is None:
            # Deployer not available (deploy_harbor not called this session) —
            # reconstruct a minimal one just for the cluster trust step.
            from daalu.bootstrap.registry.harbor_deployer import HarborDeployer
            import subprocess
            effective_kubeconfig = getattr(self.registry_cfg, "mgmt_kubeconfig", None)
            deployer = HarborDeployer(
                mgmt_kubeconfig=effective_kubeconfig or "",
                harbor_hostname=self.registry_cfg.harbor_hostname,
                admin_password=self.admin_password,
                harbor_node_ip=getattr(self.registry_cfg, "harbor_node_ip", None),
            )
            deployer._access_url = self.harbor_registry_url()
        deployer.configure_cluster_registry_trust(cluster_kubeconfig)

    def harbor_registry_url(self) -> str:
        if getattr(self, "_harbor_access_url", None):
            return self._harbor_access_url
        # If harbor_node_ip is set, the NodePort URL is the canonical access URL
        # for cluster nodes (provisioning NIC, reachable from 10.10.0.x nodes).
        node_ip = getattr(self.registry_cfg, "harbor_node_ip", None)
        if node_ip:
            return f"{node_ip}:30003"
        return self.registry_cfg.harbor_hostname

    def harbor_project(self) -> str:
        return self.registry_cfg.project
