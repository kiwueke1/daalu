# src/daalu/bootstrap/openstack/components/cinder/cinder.py

from __future__ import annotations

from pathlib import Path
from typing import Optional
import json

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.utils.helpers import build_openstack_endpoints
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager
import logging

log = logging.getLogger("daalu")


class CinderComponent(InfraComponent):
    """
    Daalu Cinder component (OpenStack Block Storage).

    Responsibilities:
    - Deploy Cinder Helm chart (API, Scheduler, Volume, Backup)
    - Configure endpoints (DB, Identity, RabbitMQ, Cache)
    - Ceph RBD backend for volumes and backups
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "cinder",
        secrets_path: Path,
        keystone_public_host: str,
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="cinder",
            repo_name="local",
            repo_url="",
            chart="cinder",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/cinder"),
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
        # Inject cinder service user auth into identity endpoint
        endpoints["identity"]["auth"]["cinder"] = {
            "role": "admin,service",
            "region_name": "RegionOne",
            "username": "cinder",
            "password": self._cinder_keystone_password,
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
        log.debug("[cinder] Starting pre-install...")

        # 1) Ensure RabbitMQ cluster for cinder
        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )
        rmq.ensure_cluster("cinder")

        # 2) Build OpenStack Helm endpoints (DB, Rabbit, Cache, Identity)
        log.debug("[cinder] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="cinder",
        )

        # 3) Read cinder keystone service password from secrets
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._cinder_keystone_password = secrets.require(
            "openstack_helm_endpoints_cinder_keystone_password"
        )
        log.debug("[cinder] OpenStack endpoints ready")

        log.debug("[cinder][DEBUG] Computed OpenStack Helm endpoints:")
        log.debug(json.dumps(self._computed_endpoints, indent=2, sort_keys=True, default=str))

        log.debug("[cinder] pre-install complete")

    # -------------------------------------------------
    # post_install
    # -------------------------------------------------
    def post_install(self, kubectl):
        log.debug("[cinder] Starting post-install...")
        self.kubectl = kubectl

        super().post_install(kubectl)

        self._wait_for_cinder_ready(kubectl)

        log.debug("[cinder] post-install complete")

    # -------------------------------------------------
    # Wait for Cinder API ready
    # -------------------------------------------------
    def _wait_for_cinder_ready(self, kubectl):
        log.debug("[cinder] Waiting for cinder-api deployment...")
        kubectl.wait_for_deployment_ready(
            name="cinder-api",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[cinder] Cinder API ready")
