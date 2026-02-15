# src/daalu/bootstrap/openstack/components/heat/heat.py

from __future__ import annotations

from pathlib import Path
import json

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.utils.helpers import build_openstack_endpoints
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager
import logging

log = logging.getLogger("daalu")


class HeatComponent(InfraComponent):
    """
    Daalu Heat component (OpenStack Orchestration).

    Deploys the Heat Helm chart providing:
    - heat-api (Orchestration API)
    - heat-cfn (CloudFormation-compatible API)
    - heat-cloudwatch (CloudWatch-compatible API)
    - heat-engine (Stack orchestration engine)
    - DB sync and rabbit-init jobs

    Pre-install:
    - Ensures RabbitMQ cluster for heat
    - Builds OpenStack endpoints (DB, RabbitMQ, Cache, Identity)
    - Reads keystone service passwords and auth encryption key
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "heat",
        secrets_path: Path,
        keystone_public_host: str,
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="heat",
            repo_name="local",
            repo_url="",
            chart="heat",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/heat"),
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

        # Inject heat service user auth into identity endpoint
        endpoints["identity"]["auth"]["heat"] = {
            "role": "admin",
            "region_name": "RegionOne",
            "username": "heat",
            "password": self._heat_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        # Heat trustee user (used for deferred operations)
        endpoints["identity"]["auth"]["heat_trustee"] = {
            "role": "admin",
            "region_name": "RegionOne",
            "username": "heat-trustee",
            "password": self._heat_trustee_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        # Heat stack domain user
        endpoints["identity"]["auth"]["heat_stack_user"] = {
            "role": "admin",
            "region_name": "RegionOne",
            "username": "heat-domain",
            "password": self._heat_stack_user_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        base["endpoints"] = endpoints

        # Inject auth encryption key and region into heat conf
        base.setdefault("conf", {})
        base["conf"].setdefault("heat", {})
        base["conf"]["heat"].setdefault("DEFAULT", {})
        base["conf"]["heat"]["DEFAULT"]["auth_encryption_key"] = self._heat_auth_encryption_key
        base["conf"]["heat"]["DEFAULT"]["region_name_for_services"] = "RegionOne"

        return base

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        log.debug("[heat] Starting pre-install...")

        # 1) Ensure RabbitMQ cluster for heat
        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )
        rmq.ensure_cluster("heat")

        # 2) Build OpenStack Helm endpoints (DB, Rabbit, Cache, Identity)
        log.debug("[heat] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="heat",
        )

        # 3) Read heat keystone service passwords from secrets
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._heat_keystone_password = secrets.require(
            "openstack_helm_endpoints_heat_keystone_password"
        )
        self._heat_trustee_keystone_password = secrets.require(
            "openstack_helm_endpoints_heat_trustee_keystone_password"
        )
        self._heat_stack_user_keystone_password = secrets.require(
            "openstack_helm_endpoints_heat_stack_user_keystone_password"
        )

        # 4) Read heat auth encryption key
        self._heat_auth_encryption_key = secrets.require(
            "heat_auth_encryption_key"
        )

        log.debug("[heat] OpenStack endpoints ready")

        log.debug("[heat][DEBUG] Computed OpenStack Helm endpoints:")
        log.debug(json.dumps(self._computed_endpoints, indent=2, sort_keys=True, default=str))

        # 5) Clean up stale jobs to avoid upgrade conflicts
        self._cleanup_stale_jobs(kubectl)

        log.debug("[heat] pre-install complete")

    def _cleanup_stale_jobs(self, kubectl):
        """Remove stale heat jobs to avoid upgrade conflicts."""
        for job_name in ("heat-db-sync", "heat-rabbit-init"):
            rc, _, _ = kubectl._run(
                f"get job {job_name} -n {self.namespace} -o name"
            )
            if rc == 0:
                log.debug(f"[heat] Deleting stale job {job_name}...")
                kubectl._run(f"delete job {job_name} -n {self.namespace}")

    # -------------------------------------------------
    # post_install
    # -------------------------------------------------
    def post_install(self, kubectl):
        log.debug("[heat] Starting post-install...")
        self.kubectl = kubectl

        super().post_install(kubectl)

        self._wait_for_heat_ready(kubectl)

        log.debug("[heat] post-install complete")

    def _wait_for_heat_ready(self, kubectl):
        log.debug("[heat] Waiting for heat-api deployment...")
        kubectl.wait_for_deployment_ready(
            name="heat-api",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[heat] Heat API ready")
