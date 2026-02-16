# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/manila/manila.py

from __future__ import annotations

from pathlib import Path
import json
import shlex
import time
from typing import Optional

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.utils.helpers import build_openstack_endpoints
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager
import logging

log = logging.getLogger("daalu")


class ManilaComponent(InfraComponent):
    """
    Daalu Manila component (OpenStack Shared File System).

    Mirrors: roles/manila/tasks/main.yml

    Pre-install:
    - Generates resources (service flavor, service image, security group)
    - Generates SSH public key and creates K8s Secret
    - Ensures RabbitMQ cluster for manila
    - Builds OpenStack endpoints (DB, RabbitMQ, Cache, Identity)
    - Reads keystone service password

    Post-install:
    - Creates Istio VirtualService for manila-api (port 8786)
    - Updates service tenant quotas to unlimited
    """

    # Service flavor defaults (matching Ansible defaults/main.yml)
    FLAVOR_NAME = "m1.manila"
    FLAVOR_VCPUS = 2
    FLAVOR_RAM = 2048
    FLAVOR_DISK = 20

    # Security group
    SERVICE_SECURITY_GROUP_NAME = "manila-service-security-group"

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "manila",
        secrets_path: Path,
        keystone_public_host: str,
        enable_argocd: bool = False,
        ssh_private_key: Optional[str] = None,
    ):
        # Derive the base domain from keystone_public_host for Istio
        parts = keystone_public_host.split(".")
        base_domain = ".".join(parts[1:]) if len(parts) > 1 else keystone_public_host

        super().__init__(
            name="manila",
            repo_name="local",
            repo_url="",
            chart="manila",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/manila"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
            istio_enabled=True,
            istio_host=f"shared-file-system.{base_domain}",
            istio_service="manila-api",
            istio_service_namespace=namespace,
            istio_service_port=8786,
            istio_expected_status=200,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self.secrets_path = secrets_path
        self.keystone_public_host = keystone_public_host
        self.wait_for_pods = True
        self.min_running_pods = 1
        self.ssh_private_key = ssh_private_key

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

        # Inject manila service user auth into identity endpoint
        endpoints["identity"]["auth"]["manila"] = {
            "role": "admin",
            "region_name": "RegionOne",
            "username": "manila",
            "password": self._manila_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        base["endpoints"] = endpoints
        return base

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
        retries: int = 10,
        delay: int = 1,
    ) -> tuple[int, str, str]:
        """Run an OpenStack CLI command inside the keystone-api pod."""
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

    # =================================================================
    # Pre-install: Generate Resources
    # (mirrors roles/manila/tasks/generate_resources.yml)
    # =================================================================
    def _generate_resources(self, kubectl):
        """Generate Manila resources (flavor, security group)."""
        log.debug("[manila] Generating Manila resources...")
        self._create_manila_flavor(kubectl)
        self._create_service_security_group(kubectl)
        log.debug("[manila] Resource generation complete")

    def _resource_exists(self, kubectl, resource_type: str, name: str) -> bool:
        """Check if an OpenStack resource exists by name."""
        rc, _, _ = self._run_openstack_cmd(
            kubectl,
            f"{resource_type} show {name} -f json",
            retries=1,
        )
        return rc == 0

    def _create_manila_flavor(self, kubectl):
        """Create Manila service instance flavor (m1.manila), skip if exists."""
        name = self.FLAVOR_NAME
        log.debug(f"[manila] Ensuring service flavor '{name}'...")
        if self._resource_exists(kubectl, "flavor", name):
            log.debug(f"[manila] Service flavor '{name}' already exists")
            return

        rc, out, err = self._run_openstack_cmd(
            kubectl,
            (
                f"flavor create {name} "
                f"--vcpus {self.FLAVOR_VCPUS} "
                f"--ram {self.FLAVOR_RAM} "
                f"--disk {self.FLAVOR_DISK} "
                f"--private "
                f"-f json"
            ),
            retries=3,
        )
        if rc == 0:
            log.debug(f"[manila] Service flavor '{name}' created successfully")
        else:
            raise RuntimeError(
                f"[manila] Failed to create service flavor: {err or out}"
            )

    def _create_service_security_group(self, kubectl):
        """Create generic share driver security group and rules."""
        sg_name = self.SERVICE_SECURITY_GROUP_NAME
        log.debug(f"[manila] Ensuring service security group '{sg_name}'...")

        if not self._resource_exists(kubectl, "security group", sg_name):
            rc, out, err = self._run_openstack_cmd(
                kubectl,
                f"security group create {sg_name} --project service -f json",
                retries=3,
            )
            if rc == 0:
                log.debug(f"[manila] Security group '{sg_name}' created successfully")
            else:
                raise RuntimeError(
                    f"[manila] Failed to create security group '{sg_name}': {err or out}"
                )
        else:
            log.debug(f"[manila] Security group '{sg_name}' already exists")

        # TCP rules: ports 22 (SSH), 111 (rpcbind), 2049 (NFS)
        for port in (22, 111, 2049):
            rc, _, err = self._run_openstack_cmd(
                kubectl,
                (
                    f"security group rule create {sg_name} "
                    f"--ingress --ethertype IPv4 "
                    f"--protocol tcp "
                    f"--dst-port {port}:{port} "
                    f"--project service"
                ),
                retries=3,
            )
            if rc == 0:
                log.debug(f"[manila] SG rule tcp/{port} created for '{sg_name}'")
            else:
                log.debug(f"[manila] SG rule tcp/{port} already exists or skipped for '{sg_name}'")

        # ICMP rule
        rc, _, err = self._run_openstack_cmd(
            kubectl,
            (
                f"security group rule create {sg_name} "
                f"--ingress --ethertype IPv4 "
                f"--protocol icmp "
                f"--project service"
            ),
            retries=3,
        )
        if rc == 0:
            log.debug(f"[manila] SG rule icmp created for '{sg_name}'")
        else:
            log.debug(f"[manila] SG rule icmp already exists or skipped for '{sg_name}'")

        log.debug(f"[manila] Service security group '{sg_name}' ready")

    # =================================================================
    # Pre-install: Generate SSH Keys
    # (mirrors roles/manila/tasks/generate_public_key.yml)
    # =================================================================
    def _generate_ssh_keys(self, kubectl):
        """Generate SSH public key from private key and create K8s Secret."""
        if not self.ssh_private_key:
            log.debug("[manila] Skipping SSH key generation (no private key configured)")
            return

        log.debug("[manila] Generating SSH key pair and creating K8s Secret...")

        import base64
        import subprocess
        import tempfile
        import os
        # Write private key to temp file and generate public key
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix="_manila_ssh_key", delete=False
            ) as tmp:
                tmp.write(self.ssh_private_key)
                if not self.ssh_private_key.endswith("\n"):
                    tmp.write("\n")
                tmp_path = tmp.name

            os.chmod(tmp_path, 0o600)

            # Generate public key from private key
            result = subprocess.run(
                ["ssh-keygen", "-y", "-f", tmp_path],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                raise RuntimeError(
                    f"ssh-keygen failed: {result.stderr}"
                )

            public_key = result.stdout.strip()
            log.debug("[manila] SSH public key generated successfully")

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        # Create K8s Secret with SSH keys
        secret_manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": f"{self.release_name}-ssh-keys",
                "namespace": self.namespace,
            },
            "type": "Opaque",
            "data": {
                "id_rsa": base64.b64encode(
                    self.ssh_private_key.encode()
                ).decode(),
                "id_rsa.pub": base64.b64encode(
                    public_key.encode()
                ).decode(),
            },
        }

        try:
            kubectl.apply_objects([secret_manifest])
            log.debug(f"[manila] SSH key Secret '{self.release_name}-ssh-keys' created successfully")
        except Exception as e:
            raise RuntimeError(
                f"[manila] Failed to create SSH key Secret: {e}"
            )

    # =================================================================
    # Post-install: Update Service Tenant Quotas
    # (mirrors "Update service tenant quotas")
    # =================================================================
    def _update_service_tenant_quotas(self, kubectl):
        """Set service project quotas to unlimited (-1)."""
        log.debug("[manila] Updating service tenant quotas to unlimited...")

        quota_args = (
            "--instances -1 --cores -1 --ram -1 "
            "--volumes -1 --gigabytes -1 "
            "--secgroups -1 --secgroup-rules -1"
        )

        rc, out, err = self._run_openstack_cmd(
            kubectl,
            f"quota set service {quota_args}",
            retries=5,
        )
        if rc == 0:
            log.debug("[manila] Service tenant quotas set to unlimited successfully")
        else:
            log.debug(f"[manila] WARNING: Failed to set service quotas: {err or out}")

    # =================================================================
    # pre_install
    # =================================================================
    def pre_install(self, kubectl):
        log.debug("[manila] Starting pre-install...")

        # 1) Ensure RabbitMQ cluster for manila
        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )
        rmq.ensure_cluster("manila")

        # 2) Build OpenStack Helm endpoints (DB, Rabbit, Cache, Identity)
        log.debug("[manila] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="manila",
        )

        # 3) Read manila keystone service password from secrets
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._manila_keystone_password = secrets.require(
            "openstack_helm_endpoints_manila_keystone_password"
        )
        log.debug("[manila] OpenStack endpoints ready")

        log.debug("[manila][DEBUG] Computed OpenStack Helm endpoints:")
        log.debug(json.dumps(self._computed_endpoints, indent=2, sort_keys=True, default=str))

        # 4) Generate resources (flavor, security group)
        self._generate_resources(kubectl)

        # 5) Generate SSH keys and create K8s Secret
        self._generate_ssh_keys(kubectl)

        # 6) Clean up stale jobs to avoid upgrade conflicts
        self._cleanup_stale_jobs(kubectl)

        log.debug("[manila] pre-install complete")

    def _cleanup_stale_jobs(self, kubectl):
        """Remove stale manila jobs to avoid upgrade conflicts."""
        for job_name in ("manila-db-sync", "manila-rabbit-init"):
            rc, _, _ = kubectl._run(
                f"get job {job_name} -n {self.namespace} -o name"
            )
            if rc == 0:
                log.debug(f"[manila] Deleting stale job {job_name}...")
                kubectl._run(f"delete job {job_name} -n {self.namespace}")

    # =================================================================
    # post_install
    # =================================================================
    def post_install(self, kubectl):
        log.debug("[manila] Starting post-install...")
        self.kubectl = kubectl

        # Parent handles Istio VirtualService + validation
        super().post_install(kubectl)

        self._wait_for_manila_ready(kubectl)

        # Update service tenant quotas (post helm deploy)
        self._update_service_tenant_quotas(kubectl)

        log.debug("[manila] post-install complete")

    def _wait_for_manila_ready(self, kubectl):
        log.debug("[manila] Waiting for manila-api deployment...")
        kubectl.wait_for_deployment_ready(
            name="manila-api",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[manila] Manila API ready")
