# src/daalu/bootstrap/openstack/components/horizon/horizon.py

from __future__ import annotations

from pathlib import Path
import json
from typing import Optional

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.utils.helpers import build_openstack_endpoints
import logging

log = logging.getLogger("daalu")


class HorizonComponent(InfraComponent):
    """
    Daalu Horizon component (OpenStack Dashboard).

    Mirrors: roles/horizon/tasks/main.yml

    Pre-install:
    - Builds OpenStack endpoints (DB, Cache, Identity)
    - Reads horizon DB password from secrets
    - Adds dashboard endpoint configuration

    Post-install:
    - Creates Istio VirtualService for dashboard (horizon-int:80)
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "horizon",
        secrets_path: Path,
        keystone_public_host: str,
        enable_argocd: bool = False,
    ):
        # Derive the base domain from keystone_public_host for Istio
        parts = keystone_public_host.split(".")
        base_domain = ".".join(parts[1:]) if len(parts) > 1 else keystone_public_host
        self._horizon_api_host = f"dashboard.{base_domain}"

        super().__init__(
            name="horizon",
            repo_name="local",
            repo_url="",
            chart="horizon",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/horizon"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
            istio_enabled=True,
            istio_host=self._horizon_api_host,
            istio_service="horizon-int",
            istio_service_namespace=namespace,
            istio_service_port=80,
            istio_expected_status=200,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self.secrets_path = secrets_path
        self.keystone_public_host = keystone_public_host
        self.wait_for_pods = True
        self.min_running_pods = 1

    # =================================================================
    # Helpers
    # =================================================================
    def assets_dir(self) -> Path:
        return self._assets_dir

    def values(self) -> dict:
        base = self.load_values_file(self.values_path)
        if not hasattr(self, "_computed_endpoints"):
            raise RuntimeError("OpenStack endpoints not computed yet")

        endpoints = dict(self._computed_endpoints)

        # Inject horizon DB password into oslo_db auth
        # (mirrors _openstack_helm_endpoints_dashboard.oslo_db.auth.horizon)
        if "oslo_db" in endpoints:
            endpoints["oslo_db"].setdefault("auth", {})
            endpoints["oslo_db"]["auth"]["horizon"] = {
                "password": self._horizon_mariadb_password,
            }

        # Add dashboard endpoint
        # (mirrors _openstack_helm_endpoints_dashboard.dashboard)
        endpoints["dashboard"] = {
            "scheme": {
                "public": "https",
            },
            "host_fqdn_override": {
                "public": {
                    "host": self._horizon_api_host,
                },
            },
            "port": {
                "api": {
                    "public": 443,
                },
            },
        }

        base["endpoints"] = endpoints

        # Inject allowed_hosts into conf
        base.setdefault("conf", {})
        base["conf"].setdefault("horizon", {})
        base["conf"]["horizon"].setdefault("local_settings", {})
        base["conf"]["horizon"]["local_settings"].setdefault("config", {})
        base["conf"]["horizon"]["local_settings"]["config"]["allowed_hosts"] = [
            self._horizon_api_host,
        ]

        # Inject SSO / WebSSO settings using the keystone public host
        # (mirrors _horizon_helm_values.conf.horizon.local_settings.config.raw)
        keystone_fqdn = self.keystone_public_host
        raw = base["conf"]["horizon"]["local_settings"]["config"].setdefault("raw", {})
        raw["WEBSSO_KEYSTONE_URL"] = f"https://{keystone_fqdn}/v3"
        raw["LOGOUT_URL"] = (
            f"https://{keystone_fqdn}/v3/auth/OS-FEDERATION/identity_providers/"
            f"redirect?logout=https://{self._horizon_api_host}/auth/logout/"
        )

        return base

    # =================================================================
    # pre_install
    # =================================================================
    def pre_install(self, kubectl):
        log.debug("[horizon] Starting pre-install...")

        # 1) Build OpenStack Helm endpoints (DB, Rabbit, Cache, Identity)
        log.debug("[horizon] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="horizon",
        )

        # 2) Read horizon DB password from secrets
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._horizon_mariadb_password = secrets.require(
            "openstack_helm_endpoints_horizon_mariadb_password"
        )
        log.debug("[horizon] OpenStack endpoints ready")

        log.debug("[horizon][DEBUG] Computed OpenStack Helm endpoints:")
        log.debug(json.dumps(self._computed_endpoints, indent=2, sort_keys=True, default=str))

        # 3) Clean up stale jobs to avoid upgrade conflicts
        self._cleanup_stale_jobs(kubectl)

        log.debug("[horizon] pre-install complete")

    def _cleanup_stale_jobs(self, kubectl):
        """Remove stale horizon jobs to avoid upgrade conflicts."""
        for job_name in ("horizon-db-sync",):
            rc, _, _ = kubectl._run(
                f"get job {job_name} -n {self.namespace} -o name"
            )
            if rc == 0:
                log.debug(f"[horizon] Deleting stale job {job_name}...")
                kubectl._run(f"delete job {job_name} -n {self.namespace}")

    # =================================================================
    # post_install
    # =================================================================
    def post_install(self, kubectl):
        log.debug("[horizon] Starting post-install...")
        self.kubectl = kubectl

        # Parent handles Istio VirtualService + validation
        super().post_install(kubectl)

        self._wait_for_horizon_ready(kubectl)

        log.debug("[horizon] post-install complete")

    def _wait_for_horizon_ready(self, kubectl):
        log.debug("[horizon] Waiting for horizon deployment...")
        kubectl.wait_for_deployment_ready(
            name="horizon",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[horizon] Horizon ready")
