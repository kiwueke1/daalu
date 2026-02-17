# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/ceilometer/ceilometer.py

from __future__ import annotations

from pathlib import Path
import json

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.utils.helpers import build_openstack_endpoints
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager
import logging

log = logging.getLogger("daalu")


class CeilometerComponent(InfraComponent):
    """
    Daalu Ceilometer component (OpenStack Telemetry).

    Deploys the Ceilometer Helm chart providing:
    - ceilometer-central (polling agent)
    - ceilometer-collector (data collection)
    - ceilometer-notification (notification agent)
    - ceilometer-compute (compute agent)
    - DB sync and rabbit-init jobs

    Pre-install:
    - Ensures RabbitMQ cluster for ceilometer
    - Builds OpenStack endpoints (DB, RabbitMQ, Cache, Identity)
    - Reads keystone service password
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "ceilometer",
        secrets_path: Path,
        keystone_public_host: str,
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="ceilometer",
            repo_name="local",
            repo_url="",
            chart="ceilometer",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/ceilometer"),
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

        # Inject ceilometer service user auth into identity endpoint
        endpoints["identity"]["auth"]["ceilometer"] = {
            "role": "admin",
            "region_name": "RegionOne",
            "username": "ceilometer",
            "password": self._ceilometer_keystone_password,
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
        log.debug("[ceilometer] Starting pre-install...")

        # 1) Ensure RabbitMQ cluster for ceilometer
        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )
        rmq.ensure_cluster("ceilometer")

        # 2) Build OpenStack Helm endpoints (DB, Rabbit, Cache, Identity)
        log.debug("[ceilometer] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="ceilometer",
        )

        # 3) Read ceilometer keystone service password from secrets
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._ceilometer_keystone_password = secrets.require(
            "openstack_helm_endpoints_ceilometer_keystone_password"
        )

        log.debug("[ceilometer] OpenStack endpoints ready")

        log.debug("[ceilometer][DEBUG] Computed OpenStack Helm endpoints:")
        log.debug(json.dumps(self._computed_endpoints, indent=2, sort_keys=True, default=str))

        # 4) Clean up stale jobs to avoid upgrade conflicts
        self._cleanup_stale_jobs(kubectl)

        log.debug("[ceilometer] pre-install complete")

    def _cleanup_stale_jobs(self, kubectl):
        """Remove stale ceilometer jobs to avoid upgrade conflicts."""
        for job_name in ("ceilometer-db-sync", "ceilometer-rabbit-init"):
            rc, _, _ = kubectl._run(
                f"get job {job_name} -n {self.namespace} -o name"
            )
            if rc == 0:
                log.debug(f"[ceilometer] Deleting stale job {job_name}...")
                kubectl._run(f"delete job {job_name} -n {self.namespace}")

    # -------------------------------------------------
    # post_install
    # -------------------------------------------------
    def post_install(self, kubectl):
        log.debug("[ceilometer] Starting post-install...")
        self.kubectl = kubectl

        super().post_install(kubectl)

        self._wait_for_ceilometer_ready(kubectl)

        log.debug("[ceilometer] post-install complete")

    def _wait_for_ceilometer_ready(self, kubectl):
        log.debug("[ceilometer] Waiting for ceilometer-collector deployment...")
        kubectl.wait_for_deployment_ready(
            name="ceilometer-collector",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[ceilometer] Ceilometer collector ready")
