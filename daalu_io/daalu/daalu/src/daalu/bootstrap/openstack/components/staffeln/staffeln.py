# src/daalu/bootstrap/openstack/components/staffeln/staffeln.py

from __future__ import annotations

from pathlib import Path
import json

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.utils.helpers import build_openstack_endpoints
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager
import logging

log = logging.getLogger("daalu")


class StaffelnComponent(InfraComponent):
    """
    Daalu Staffeln component (OpenStack Backup Service for Cinder volumes).

    Responsibilities:
    - Deploy Staffeln Helm chart (API + Conductor)
    - Configure endpoints (DB, Identity, RabbitMQ, Cache)
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "staffeln",
        secrets_path: Path,
        keystone_public_host: str,
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="staffeln",
            repo_name="local",
            repo_url="",
            chart="staffeln",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/staffeln"),
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
        base["endpoints"] = dict(self._computed_endpoints)
        return base

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        log.debug("[staffeln] Starting pre-install...")

        # 1) Ensure RabbitMQ cluster for staffeln
        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )
        rmq.ensure_cluster("staffeln")

        # 2) Build OpenStack Helm endpoints (DB, Rabbit, Cache, Identity)
        log.debug("[staffeln] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="staffeln",
        )
        log.debug("[staffeln] OpenStack endpoints ready")

        log.debug("[staffeln][DEBUG] Computed OpenStack Helm endpoints:")
        log.debug(json.dumps(self._computed_endpoints, indent=2, sort_keys=True, default=str))

        log.debug("[staffeln] pre-install complete")

    # -------------------------------------------------
    # post_install
    # -------------------------------------------------
    def post_install(self, kubectl):
        log.debug("[staffeln] Starting post-install...")
        self.kubectl = kubectl

        super().post_install(kubectl)

        self._wait_for_staffeln_ready(kubectl)

        log.debug("[staffeln] post-install complete")

    # -------------------------------------------------
    # Wait for Staffeln API ready
    # -------------------------------------------------
    def _wait_for_staffeln_ready(self, kubectl):
        log.debug("[staffeln] Waiting for staffeln-api deployment...")
        kubectl.wait_for_deployment_ready(
            name="staffeln-api",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[staffeln] Staffeln API ready")
