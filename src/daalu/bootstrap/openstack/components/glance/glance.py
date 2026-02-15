# src/daalu/bootstrap/openstack/components/glance/glance.py

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Dict
from dataclasses import dataclass
import json
import shlex

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.bootstrap.openstack.images import GlanceImageSpec
from daalu.utils.helpers import build_openstack_endpoints
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager
import logging

log = logging.getLogger("daalu")

class GlanceComponent(InfraComponent):
    """
    Daalu Glance component.

    Responsibilities:
    - Deploy Glance Helm chart
    - Expose API via Istio / Ingress
    - Bootstrap images into Glance
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "glance",
        glance_public_host: str,
        secrets_path: Path,
        images: Optional[List[GlanceImageSpec]] = None,
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="glance",
            repo_name="local",
            repo_url="",
            chart="glance",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/glance"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
            istio_enabled=True,
            istio_host=glance_public_host,
            istio_service="glance-api",
            istio_service_namespace=namespace,
            istio_service_port=9292,
            istio_expected_status=200,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self.secrets_path = secrets_path
        self.glance_public_host = glance_public_host
        self.images = images or []
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
        # Inject glance service user auth into identity endpoint
        # (needed by keystone authtoken middleware in glance)
        endpoints["identity"]["auth"]["glance"] = {
            "role": "admin",
            "region_name": "RegionOne",
            "username": "glance",
            "password": self._glance_keystone_password,
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
        log.debug("[glance] Starting pre-install...")

        # 1) Ensure RabbitMQ cluster for glance
        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )
        rmq.ensure_cluster("glance")

        # 2) Build OpenStack Helm endpoints (DB, Rabbit, Cache, Identity)
        log.debug("[glance] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.glance_public_host,
            service="glance",
        )

        # 3) Read glance keystone service password from secrets
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._glance_keystone_password = secrets.require(
            "openstack_helm_endpoints_glance_keystone_password"
        )
        log.debug("[glance] OpenStack endpoints ready")

        log.debug("[glance][DEBUG] Computed OpenStack Helm endpoints:")
        log.debug(json.dumps(self._computed_endpoints, indent=2, sort_keys=True, default=str))

        log.debug("[glance] pre-install complete")

    # -------------------------------------------------
    # post_install
    # -------------------------------------------------
    def post_install(self, kubectl):
        log.debug("[glance] Starting post-install...")
        self.kubectl = kubectl

        # Parent handles Istio VirtualService + validation
        super().post_install(kubectl)

        # Wait for glance-api deployment
        self._wait_for_glance_ready(kubectl)

        # Bootstrap images if any
        if self.images:
            self._bootstrap_images(kubectl)

        log.debug("[glance] post-install complete")

    # -------------------------------------------------
    # Wait for Glance API ready
    # -------------------------------------------------
    def _wait_for_glance_ready(self, kubectl):
        log.debug("[glance] Waiting for glance-api deployment...")
        kubectl.wait_for_deployment_ready(
            name="glance-api",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[glance] Glance API ready")

    # -------------------------------------------------
    # Bootstrap images
    # -------------------------------------------------
    def _bootstrap_images(self, kubectl):
        log.debug("[glance] Bootstrapping images...")
        pod = self._get_keystone_api_pod()
        openrc = self._build_openrc_env()
        env_prefix = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in openrc.items()
        )

        for image in self.images:
            self._ensure_image(kubectl, pod, env_prefix, image)

        log.debug("[glance] Images bootstrapped")

    def _ensure_image(self, kubectl, pod, env_prefix, image: GlanceImageSpec):
        # Check if image already exists
        check_cmd = (
            f"exec {pod} -n {self.namespace} -c keystone-api -- "
            f"env {env_prefix} "
            f"openstack image show {shlex.quote(image.name)} -f json"
        )
        rc, out, err = kubectl._run(check_cmd)
        if rc == 0:
            log.debug(f"[glance] Image '{image.name}' already exists")
            return

        # Build create command
        create_parts = [
            f"exec {pod} -n {self.namespace} -c keystone-api --",
            f"env {env_prefix}",
            f"openstack image create {shlex.quote(image.name)}",
        ]

        if image.disk_format:
            create_parts.append(f"--disk-format {image.disk_format}")
        if image.container_format:
            create_parts.append(f"--container-format {image.container_format}")
        if image.is_public:
            create_parts.append("--public")
        if image.min_disk is not None:
            create_parts.append(f"--min-disk {image.min_disk}")
        if image.min_ram is not None:
            create_parts.append(f"--min-ram {image.min_ram}")

        create_cmd = " ".join(create_parts)
        rc, out, err = kubectl._run(create_cmd)
        if rc != 0:
            raise RuntimeError(
                f"Failed to create image '{image.name}': {err or out}"
            )
        log.debug(f"[glance] Image '{image.name}' created")

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
