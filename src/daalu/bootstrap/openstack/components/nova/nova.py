# src/daalu/bootstrap/openstack/components/nova/nova.py

from __future__ import annotations

from pathlib import Path
from typing import Optional
import json
import shlex
import time

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.utils.helpers import build_openstack_endpoints
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager
import logging

log = logging.getLogger("daalu")


class NovaComponent(InfraComponent):
    """
    Daalu Nova component (OpenStack Compute).

    Deploys the Nova Helm chart providing:
    - nova-api-osapi (Compute API)
    - nova-api-metadata (Metadata API)
    - nova-conductor
    - nova-scheduler
    - nova-novncproxy (VNC console proxy)
    - nova-compute (DaemonSet on compute nodes)
    - Cell setup and DB sync jobs

    Pre-install:
    - Ensures RabbitMQ cluster for nova
    - Builds OpenStack endpoints (DB, RabbitMQ, Cache, Identity)
    - Reads keystone service password
    - Cleans up stale bootstrap/cell-setup jobs

    Post-install:
    - Creates Istio VirtualService for compute API (port 8774)
    - Creates Istio VirtualService for novncproxy (port 6080)
    - Creates compute flavors
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "nova",
        secrets_path: Path,
        keystone_public_host: str,
        network_backend: str = "ovn",
        enable_argocd: bool = False,
        nova_flavors: list[dict] | None = None,
    ):
        # Derive the base domain from keystone_public_host for Istio
        parts = keystone_public_host.split(".")
        base_domain = ".".join(parts[1:]) if len(parts) > 1 else keystone_public_host

        super().__init__(
            name="nova",
            repo_name="local",
            repo_url="",
            chart="nova",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/nova"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
            istio_enabled=True,
            istio_host=f"compute.{base_domain}",
            istio_service="nova-api",
            istio_service_namespace=namespace,
            istio_service_port=8774,
            istio_expected_status=200,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self.secrets_path = secrets_path
        self.keystone_public_host = keystone_public_host
        self.network_backend = network_backend
        self.nova_flavors = nova_flavors or []
        self.base_domain = base_domain
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

        # Inject nova service user auth into identity endpoint
        endpoints["identity"]["auth"]["nova"] = {
            "role": "admin,service",
            "region_name": "RegionOne",
            "username": "nova",
            "password": self._nova_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        # Inject placement service user auth (nova conductor needs it for [placement])
        endpoints["identity"]["auth"]["placement"] = {
            "role": "admin,service",
            "region_name": "RegionOne",
            "username": "placement",
            "password": self._placement_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        # Inject neutron service user auth (nova needs it for [neutron] section)
        endpoints["identity"]["auth"]["neutron"] = {
            "region_name": "RegionOne",
            "username": "neutron",
            "password": self._neutron_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        # Inject ironic service user auth (nova needs it for [ironic] section)
        endpoints["identity"]["auth"]["ironic"] = {
            "auth_type": "password",
            "auth_version": "v3",
            "region_name": "RegionOne",
            "username": "ironic",
            "password": self._ironic_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        # Inject cinder service user auth (nova needs it for [cinder] section)
        endpoints["identity"]["auth"]["cinder"] = {
            "role": "admin,service",
            "region_name": "RegionOne",
            "username": "cinder",
            "password": self._cinder_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        # Nova needs 3 DB endpoints: oslo_db (nova), oslo_db_api (nova_api),
        # oslo_db_cell0 (nova_cell0). Build them from the base oslo_db.
        oslo_db = endpoints["oslo_db"]
        db_auth_admin = oslo_db["auth"]["admin"]
        db_host = oslo_db["hosts"]["default"]
        db_scheme = oslo_db["scheme"]
        db_port = oslo_db["port"]

        endpoints["oslo_db_api"] = {
            "namespace": None,
            "auth": {
                "admin": db_auth_admin,
                "nova": {
                    "username": "nova",
                    "password": oslo_db["auth"]["nova"]["password"],
                },
            },
            "hosts": {"default": db_host},
            "path": "/nova_api",
            "scheme": db_scheme,
            "port": db_port,
        }

        endpoints["oslo_db_cell0"] = {
            "namespace": None,
            "auth": {
                "admin": db_auth_admin,
                "nova": {
                    "username": "nova",
                    "password": oslo_db["auth"]["nova"]["password"],
                },
            },
            "hosts": {"default": db_host},
            "path": "/nova_cell0",
            "scheme": db_scheme,
            "port": db_port,
        }

        base["endpoints"] = endpoints

        # Set network backend
        base.setdefault("network", {})
        base["network"]["backend"] = [self.network_backend]

        return base

    # -------------------------------------------------
    # OpenStack CLI helpers
    # -------------------------------------------------
    def _build_openrc_env(self) -> dict[str, str]:
        """Build OS_* env vars from the computed endpoints for OpenStack CLI."""
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

    def _get_keystone_api_pod(self, kubectl) -> str:
        """Find a running keystone-api pod for CLI execution."""
        pods = kubectl.get_pods(self.namespace)
        for pod in pods:
            labels = pod.get("metadata", {}).get("labels", {})
            if (
                labels.get("application") == "keystone"
                and labels.get("component") == "api"
            ):
                return pod["metadata"]["name"]
        raise RuntimeError("No keystone-api pod found")

    def _run_openstack_cmd(
        self,
        kubectl,
        cmd: str,
        *,
        retries: int = 60,
        delay: int = 5,
    ) -> tuple[int, str, str]:
        """Run an OpenStack CLI command inside the keystone-api pod with retries."""
        pod = self._get_keystone_api_pod(kubectl)
        openrc = self._build_openrc_env()
        env_prefix = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in openrc.items()
        )

        full_cmd = (
            f"exec {pod} -n {self.namespace} -c keystone-api -- "
            f"env {env_prefix} "
            f"openstack {cmd}"
        )

        for attempt in range(retries):
            rc, out, err = kubectl._run(full_cmd)
            if rc == 0:
                return rc, out, err
            if attempt < retries - 1:
                time.sleep(delay)

        return rc, out, err

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        log.debug("[nova] Starting pre-install...")

        # 1) Ensure RabbitMQ cluster for nova
        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )
        rmq.ensure_cluster("nova")

        # 2) Build OpenStack Helm endpoints (DB, Rabbit, Cache, Identity)
        log.debug("[nova] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="nova",
        )

        # 3) Read nova keystone service password from secrets
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._nova_keystone_password = secrets.require(
            "openstack_helm_endpoints_nova_keystone_password"
        )

        # 4) Read placement keystone service password (nova needs it for [placement] auth)
        self._placement_keystone_password = secrets.require(
            "openstack_helm_endpoints_placement_keystone_password"
        )

        # 5) Read neutron keystone service password (nova [neutron] section)
        self._neutron_keystone_password = secrets.require(
            "openstack_helm_endpoints_neutron_keystone_password"
        )

        # 6) Read ironic keystone service password (nova [ironic] section)
        self._ironic_keystone_password = secrets.get(
            "openstack_helm_endpoints_ironic_keystone_password",
            "password",
        )

        # 7) Read cinder keystone service password (nova [cinder] section)
        self._cinder_keystone_password = secrets.require(
            "openstack_helm_endpoints_cinder_keystone_password"
        )

        # 8) Read metadata proxy shared secret
        self._metadata_secret = secrets.get(
            "openstack_helm_endpoints_compute_metadata_secret",
            "",
        )

        log.debug("[nova] OpenStack endpoints ready")

        log.debug("[nova][DEBUG] Computed OpenStack Helm endpoints:")
        log.debug(json.dumps(self._computed_endpoints, indent=2, sort_keys=True, default=str))

        # 6) Clean up stale bootstrap and cell-setup jobs
        self._cleanup_stale_jobs(kubectl)

        log.debug("[nova] pre-install complete")

    def _cleanup_stale_jobs(self, kubectl):
        """Remove stale nova-bootstrap and nova-cell-setup jobs to avoid upgrade conflicts."""
        for job_name in ("nova-bootstrap", "nova-cell-setup"):
            rc, _, _ = kubectl._run(
                f"get job {job_name} -n {self.namespace} -o name"
            )
            if rc == 0:
                log.debug(f"[nova] Deleting stale job {job_name}...")
                kubectl._run(f"delete job {job_name} -n {self.namespace}")

    # -------------------------------------------------
    # post_install
    # -------------------------------------------------
    def post_install(self, kubectl):
        log.debug("[nova] Starting post-install...")
        self.kubectl = kubectl

        # Parent handles primary Istio VirtualService (compute API) + validation
        super().post_install(kubectl)

        # Create second VirtualService for novncproxy
        self._create_novncproxy_virtualservice(kubectl)

        self._wait_for_nova_ready(kubectl)

        # Create compute flavors (mirrors Ansible "Create flavors" block)
        if self.nova_flavors:
            self._create_flavors(kubectl)

        log.debug("[nova] post-install complete")

    def _wait_for_nova_ready(self, kubectl):
        log.debug("[nova] Waiting for nova-api-osapi deployment...")
        kubectl.wait_for_deployment_ready(
            name="nova-api-osapi",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[nova] Nova API ready")

    # -------------------------------------------------
    # Ingress: novncproxy VirtualService
    # -------------------------------------------------
    def _create_novncproxy_virtualservice(self, kubectl):
        """Create Istio VirtualService for nova-novncproxy (port 6080)."""
        vs_name = "nova-novncproxy-vs"

        if kubectl.resource_exists(
            kind="virtualservice.networking.istio.io",
            name=vs_name,
            namespace=self.istio_namespace,
        ):
            log.debug(f"[nova] novncproxy VirtualService already exists")
            return

        log.debug("[nova] Creating Istio VirtualService for novncproxy...")

        manifest = {
            "apiVersion": "networking.istio.io/v1beta1",
            "kind": "VirtualService",
            "metadata": {
                "name": vs_name,
                "namespace": self.istio_namespace,
            },
            "spec": {
                "gateways": [self.istio_gateway],
                "hosts": [f"console.{self.base_domain}"],
                "http": [
                    {
                        "match": [{"uri": {"prefix": "/"}}],
                        "route": [
                            {
                                "destination": {
                                    "host": (
                                        f"nova-novncproxy."
                                        f"{self.namespace}."
                                        "svc.cluster.local"
                                    ),
                                    "port": {"number": 6080},
                                }
                            }
                        ],
                    }
                ],
            },
        }

        kubectl.apply_objects([manifest])
        log.debug("[nova] novncproxy VirtualService created")

    # -------------------------------------------------
    # Create flavors
    # -------------------------------------------------
    def _create_flavors(self, kubectl):
        """
        Create compute flavors via OpenStack CLI.

        Mirrors Ansible task "Create flavors" with retry logic (retries=60, delay=5)
        because the nova API often returns 503 right after becoming ready.

        Each flavor dict supports keys:
          name, vcpus, ram, disk, ephemeral, swap, is_public, flavorid, extra_specs
        """
        log.debug(f"[nova] Creating {len(self.nova_flavors)} flavor(s)...")

        for flavor in self.nova_flavors:
            name = flavor["name"]
            vcpus = flavor["vcpus"]
            ram = flavor["ram"]

            cmd_parts = [
                f"flavor create {shlex.quote(name)}",
                f"--vcpus {vcpus}",
                f"--ram {ram}",
            ]

            if "disk" in flavor:
                cmd_parts.append(f"--disk {flavor['disk']}")
            if "ephemeral" in flavor:
                cmd_parts.append(f"--ephemeral {flavor['ephemeral']}")
            if "swap" in flavor:
                cmd_parts.append(f"--swap {flavor['swap']}")
            if "flavorid" in flavor:
                cmd_parts.append(f"--id {shlex.quote(str(flavor['flavorid']))}")
            if flavor.get("is_public") is False:
                cmd_parts.append("--private")
            if "rxtx_factor" in flavor:
                cmd_parts.append(f"--rxtx-factor {flavor['rxtx_factor']}")

            cmd_parts.append("-f json")
            cmd = " ".join(cmd_parts)

            log.debug(f"[nova] Creating flavor '{name}'...")
            rc, out, err = self._run_openstack_cmd(
                kubectl, cmd, retries=60, delay=5,
            )

            if rc == 0:
                log.debug(f"[nova] Flavor '{name}' created successfully")
            elif "already exists" in (err or "").lower() or "conflict" in (err or "").lower():
                log.debug(f"[nova] Flavor '{name}' already exists, skipping")
            else:
                raise RuntimeError(
                    f"[nova] Failed to create flavor '{name}': {err or out}"
                )

            # Apply extra_specs if provided
            if flavor.get("extra_specs"):
                self._set_flavor_extra_specs(kubectl, name, flavor["extra_specs"])

        log.debug("[nova] All flavors created")

    def _set_flavor_extra_specs(self, kubectl, flavor_name: str, extra_specs: dict):
        """Set extra specs on a flavor."""
        specs_str = " ".join(
            f"{shlex.quote(k)}={shlex.quote(str(v))}"
            for k, v in extra_specs.items()
        )
        cmd = f"flavor set {shlex.quote(flavor_name)} --property {specs_str}"

        rc, out, err = self._run_openstack_cmd(
            kubectl, cmd, retries=5, delay=2,
        )
        if rc == 0:
            log.debug(f"[nova] Extra specs set on flavor '{flavor_name}'")
        else:
            log.debug(f"[nova] WARNING: Failed to set extra specs on '{flavor_name}': {err or out}")
