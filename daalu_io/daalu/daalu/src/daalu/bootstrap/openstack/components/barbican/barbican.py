from pathlib import Path
import json
import shlex
from typing import Any

from daalu.bootstrap.engine.component import InfraComponent
from daalu.utils.helpers import build_openstack_endpoints
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager
import logging

log = logging.getLogger("daalu")


class BarbicanComponent(InfraComponent):
    """
    Daalu Barbican component (OpenStack Key Manager).

    Mirrors: roles/barbican/tasks/main.yml
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        secrets_path: Path,
        keystone_public_host: str,
    ):
        super().__init__(
            name="barbican",
            repo_name="local",
            repo_url="",
            chart="barbican",
            version=None,
            namespace=namespace,
            release_name="barbican",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/barbican"),
            kubeconfig=kubeconfig,
            istio_enabled=True,
            istio_host="barbican.daalu.io",
            istio_service="barbican-api",
            istio_service_namespace="openstack",
            istio_service_port=9311,
            istio_expected_status=401,
        )
        self.values_path = values_path
        self._assets_dir = assets_dir
        self.secrets_path = secrets_path
        self.keystone_public_host = keystone_public_host
        self.wait_for_pods = True
        self.min_running_pods = 1

    def assets_dir(self) -> Path:
        return self._assets_dir

    def values(self) -> dict:
        base = self.load_values_file(self.values_path)
        if not hasattr(self, "_computed_endpoints"):
            raise RuntimeError("OpenStack endpoints not computed yet")
        endpoints = dict(self._computed_endpoints)
        # Inject barbican service user auth into identity endpoint
        # (needed by keystone authtoken middleware in barbican)
        endpoints["identity"]["auth"]["barbican"] = {
            "role": "admin",
            "region_name": "RegionOne",
            "username": "barbican",
            "password": self._barbican_keystone_password,
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
        log.debug("[barbican] Starting pre-install...")

        # 1) Ensure RabbitMQ cluster for barbican
        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )
        rmq.ensure_cluster("barbican")

        # 2) Build OpenStack Helm endpoints (DB, Rabbit, Cache, Identity)
        log.debug("[barbican] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="barbican",
        )
        from daalu.bootstrap.openstack.secrets_manager import SecretsManager
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._barbican_keystone_password = secrets.require(
            "openstack_helm_endpoints_barbican_keystone_password"
        )
        log.debug("[barbican] OpenStack endpoints ready")

        log.debug("[barbican] pre-install complete")

    # -------------------------------------------------
    # post_install
    # -------------------------------------------------
    def post_install(self, kubectl):
        log.debug("[barbican] Starting post-install...")
        self.kubectl = kubectl

        # Parent handles Istio VirtualService + validation
        super().post_install(kubectl)

        # Wait for barbican-api deployment
        self._wait_for_barbican_ready(kubectl)

        # Create 'creator' role and implied role
        self._create_creator_role(kubectl)
        self._add_implied_roles(kubectl)

        log.debug("[barbican] post-install complete")

    # -------------------------------------------------
    # Wait for Barbican API ready
    # -------------------------------------------------
    def _wait_for_barbican_ready(self, kubectl):
        log.debug("[barbican] Waiting for barbican-api deployment...")
        kubectl.wait_for_deployment_ready(
            name="barbican-api",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[barbican] Barbican API ready")

    # -------------------------------------------------
    # Create 'creator' role in Keystone
    # -------------------------------------------------
    def _create_creator_role(self, kubectl):
        log.debug("[barbican] Creating 'creator' role in Keystone...")
        pod = self._get_keystone_api_pod()
        openrc = self._build_openrc_env()
        env_prefix = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in openrc.items()
        )

        # Check if role already exists
        check_cmd = (
            f"exec {pod} -n {self.namespace} -c keystone-api -- "
            f"env {env_prefix} "
            f"openstack role show creator -f json"
        )
        rc, out, err = kubectl._run(check_cmd)
        if rc == 0:
            log.debug("[barbican] Role 'creator' already exists")
            return

        create_cmd = (
            f"exec {pod} -n {self.namespace} -c keystone-api -- "
            f"env {env_prefix} "
            f"openstack role create creator"
        )
        rc, out, err = kubectl._run(create_cmd)
        if rc != 0:
            raise RuntimeError(
                f"Failed to create 'creator' role: {err or out}"
            )
        log.debug("[barbican] Role 'creator' created")

    # -------------------------------------------------
    # Add implied roles: member implies creator
    # -------------------------------------------------
    def _add_implied_roles(self, kubectl):
        log.debug("[barbican] Adding implied role: member -> creator...")
        pod = self._get_keystone_api_pod()
        openrc = self._build_openrc_env()
        env_prefix = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in openrc.items()
        )

        cmd = (
            f"exec {pod} -n {self.namespace} -c keystone-api -- "
            f"env {env_prefix} "
            f"openstack implied role create --implied-role creator member"
        )
        rc, out, err = kubectl._run(cmd)
        if rc != 0:
            if "Duplicate entry" in (err or "") or "Duplicate entry" in (out or ""):
                log.debug("[barbican] Implied role already exists")
                return
            raise RuntimeError(
                f"Failed to add implied role: {err or out}"
            )
        log.debug("[barbican] Implied role member -> creator created")

    # -------------------------------------------------
    # Helpers (reuse keystone-api pod for openstack CLI)
    # -------------------------------------------------
    def _get_keystone_api_pod(self) -> str:
        pods = self.kubectl.get_pods(self.namespace)
        for pod in pods:
            labels = pod.get("metadata", {}).get("labels", {})
            if (
                labels.get("application") == "keystone"
                and labels.get("component") == "api"
            ):
                return pod["metadata"]["name"]
        raise RuntimeError("No keystone-api pod found")

    def _build_openrc_env(self) -> dict[str, str]:
        admin = self._computed_endpoints["identity"]["auth"]["admin"]
        host = self._computed_endpoints["identity"]["hosts"]["default"]
        port = self._computed_endpoints["identity"]["port"]["api"]["default"]
        return {
            "OS_IDENTITY_API_VERSION": "3",
            "OS_AUTH_URL": f"http://{host}:{port}/v3",
            "OS_REGION_NAME": admin["region_name"],
            "OS_INTERFACE": "internal",
            "OS_PROJECT_DOMAIN_NAME": admin["project_domain_name"],
            "OS_PROJECT_NAME": admin["project_name"],
            "OS_USER_DOMAIN_NAME": admin["user_domain_name"],
            "OS_USERNAME": admin["username"],
            "OS_PASSWORD": admin["password"],
            "OS_DEFAULT_DOMAIN": admin.get("default_domain_id", "default"),
        }