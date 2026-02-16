# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/placement/placement.py

from __future__ import annotations

from pathlib import Path
import json

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.utils.helpers import build_openstack_endpoints
import logging

log = logging.getLogger("daalu")


class PlacementComponent(InfraComponent):
    """
    Daalu Placement component (OpenStack Placement API).

    Responsibilities:
    - Deploy Placement Helm chart
    - Configure endpoints (DB, Identity, Cache)
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "placement",
        secrets_path: Path,
        keystone_public_host: str,
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="placement",
            repo_name="local",
            repo_url="",
            chart="placement",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/placement"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self.secrets_path = secrets_path
        self.keystone_public_host = keystone_public_host
        self.wait_for_pods = True
        self.min_running_pods = 1

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------
    def assets_dir(self) -> Path:
        return self._assets_dir

    def values(self) -> dict:
        base = self.load_values_file(self.values_path)
        if not hasattr(self, "_computed_endpoints"):
            raise RuntimeError("OpenStack endpoints not computed yet")
        endpoints = dict(self._computed_endpoints)
        # Inject placement service user auth into identity endpoint
        endpoints["identity"]["auth"]["placement"] = {
            "role": "admin",
            "region_name": "RegionOne",
            "username": "placement",
            "password": self._placement_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }
        base["endpoints"] = endpoints
        return base

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        log.debug("[placement] Starting pre-install...")

        # 1) Build OpenStack Helm endpoints (DB, Cache, Identity)
        log.debug("[placement] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="placement",
        )

        # 2) Read placement keystone service password from secrets
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._placement_keystone_password = secrets.require(
            "openstack_helm_endpoints_placement_keystone_password"
        )
        log.debug("[placement] OpenStack endpoints ready")

        log.debug("[placement][DEBUG] Computed OpenStack Helm endpoints:")
        log.debug(json.dumps(self._computed_endpoints, indent=2, sort_keys=True, default=str))

        log.debug("[placement] pre-install complete")

    # -------------------------------------------------
    # post_install
    # -------------------------------------------------
    def post_install(self, kubectl):
        log.debug("[placement] Starting post-install...")
        self.kubectl = kubectl

        super().post_install(kubectl)

        self._wait_for_placement_ready(kubectl)

        log.debug("[placement] post-install complete")

    # -------------------------------------------------
    # Wait for Placement API ready
    # -------------------------------------------------
    def _wait_for_placement_ready(self, kubectl):
        log.debug("[placement] Waiting for placement-api deployment...")
        kubectl.wait_for_deployment_ready(
            name="placement-api",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[placement] Placement API ready")
