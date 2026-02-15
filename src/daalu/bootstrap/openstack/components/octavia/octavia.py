# src/daalu/bootstrap/openstack/components/octavia/octavia.py

from __future__ import annotations

from pathlib import Path
import base64
import copy
import json
import shlex
import time

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.utils.helpers import build_openstack_endpoints
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager
import logging

log = logging.getLogger("daalu")


class OctaviaComponent(InfraComponent):
    """
    Daalu Octavia component (OpenStack Load Balancer as a Service).

    Mirrors: roles/octavia/tasks/main.yml

    Pre-install:
    - Ensures RabbitMQ cluster for octavia
    - Builds OpenStack endpoints (DB, RabbitMQ, Cache, Identity)
    - Reads keystone service password
    - Generates resources (management network, subnet, security groups,
      flavor, amphora image, SSH key)
    - Creates cert-manager CAs and Issuers
    - Creates client certificate
    - Creates admin compute quotaset

    Post-install:
    - Adds implied roles (load-balancer_member, load-balancer_observer)
    - Creates Istio VirtualService for octavia-api (port 9876)
    """

    # TLS defaults (matching Ansible defaults/main.yml)
    TLS_SERVER_COMMON_NAME = "octavia-server"
    TLS_SERVER_PRIVATE_KEY_ALGORITHM = "ECDSA"
    TLS_SERVER_PRIVATE_KEY_SIZE = 256
    TLS_CLIENT_COMMON_NAME = "octavia-client"
    TLS_CLIENT_PRIVATE_KEY_ALGORITHM = "ECDSA"
    TLS_CLIENT_PRIVATE_KEY_SIZE = 256

    # Management network defaults
    MANAGEMENT_NETWORK_NAME = "lb-mgmt-net"
    MANAGEMENT_SUBNET_NAME = "lb-mgmt-subnet"
    MANAGEMENT_SUBNET_CIDR = "172.24.0.0/22"

    # Amphora defaults
    AMPHORA_SECURITY_GROUP_NAME = "lb-mgmt-sec-grp"
    AMPHORA_FLAVOR_NAME = "m1.amphora"
    AMPHORA_FLAVOR_VCPUS = 2
    AMPHORA_FLAVOR_RAM = 2048
    AMPHORA_FLAVOR_DISK = 0

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "octavia",
        secrets_path: Path,
        keystone_public_host: str,
        enable_argocd: bool = False,
    ):
        # Derive the base domain from keystone_public_host for Istio
        parts = keystone_public_host.split(".")
        base_domain = ".".join(parts[1:]) if len(parts) > 1 else keystone_public_host

        super().__init__(
            name="octavia",
            repo_name="local",
            repo_url="",
            chart="octavia",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/octavia"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
            istio_enabled=True,
            istio_host=f"load-balancer.{base_domain}",
            istio_service="octavia-api",
            istio_service_namespace=namespace,
            istio_service_port=9876,
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

        # Inject octavia service user auth into identity endpoint
        endpoints["identity"]["auth"]["octavia"] = {
            "role": "admin",
            "region_name": "RegionOne",
            "username": "octavia",
            "password": self._octavia_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        # oslo_db_persistence: Octavia uses a separate persistence DB
        # (jobboard). Must point to Percona XtraDB, not the chart default
        # mariadb.
        if "oslo_db" in endpoints:
            endpoints["oslo_db_persistence"] = copy.deepcopy(endpoints["oslo_db"])
            endpoints["oslo_db_persistence"]["path"] = "/octavia_persistence"

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
        ignore_duplicate: bool = False,
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
            if ignore_duplicate and "Duplicate entry" in (err or ""):
                return rc, out, err
            if attempt < retries - 1:
                time.sleep(delay)

        return rc, out, err

    # =================================================================
    # Pre-install: Generate Resources
    # (mirrors roles/octavia/tasks/generate_resources.yml)
    # =================================================================
    def _generate_resources(self, kubectl):
        """Generate Octavia resources (management network, security groups, etc.)."""
        log.info("[octavia] Creating management network...")
        self._create_management_network(kubectl)
        log.info("[octavia] Creating management subnet...")
        self._create_management_subnet(kubectl)
        log.info("[octavia] Creating health-manager security group...")
        self._create_health_manager_security_group(kubectl)
        log.info("[octavia] Creating amphora security group...")
        self._create_amphora_security_group(kubectl)
        log.info("[octavia] Creating amphora flavor...")
        self._create_amphora_flavor(kubectl)
        log.info("[octavia] Creating amphora SSH key...")
        self._create_amphora_ssh_key(kubectl)
        log.info("[octavia] Resource generation complete")

    def _resource_exists(self, kubectl, resource_type: str, name: str) -> bool:
        """Check if an OpenStack resource exists by name."""
        rc, _, _ = self._run_openstack_cmd(
            kubectl,
            f"{resource_type} show {name} -f json",
            retries=1,
        )
        return rc == 0

    def _create_management_network(self, kubectl):
        """Create Octavia management network (lb-mgmt-net), skip if exists."""
        name = self.MANAGEMENT_NETWORK_NAME
        log.debug(f"[octavia] Ensuring management network '{name}'...")
        if self._resource_exists(kubectl, "network", name):
            log.debug(f"[octavia] Management network '{name}' already exists")
            return

        rc, out, err = self._run_openstack_cmd(
            kubectl,
            f"network create {name} -f json",
            retries=3,
        )
        if rc == 0:
            log.debug(f"[octavia] Management network '{name}' created successfully")
        else:
            raise RuntimeError(
                f"[octavia] Failed to create management network: {err or out}"
            )

    def _create_management_subnet(self, kubectl):
        """Create Octavia management subnet (lb-mgmt-subnet), skip if exists."""
        name = self.MANAGEMENT_SUBNET_NAME
        log.debug(f"[octavia] Ensuring management subnet '{name}'...")
        if self._resource_exists(kubectl, "subnet", name):
            log.debug(f"[octavia] Management subnet '{name}' already exists")
            return

        rc, out, err = self._run_openstack_cmd(
            kubectl,
            (
                f"subnet create {name} "
                f"--network {self.MANAGEMENT_NETWORK_NAME} "
                f"--subnet-range {self.MANAGEMENT_SUBNET_CIDR} "
                f"--gateway none "
                f"-f json"
            ),
            retries=3,
        )
        if rc == 0:
            log.debug(f"[octavia] Management subnet '{name}' created successfully")
        else:
            raise RuntimeError(
                f"[octavia] Failed to create management subnet: {err or out}"
            )

    def _ensure_security_group(self, kubectl, sg_name: str) -> None:
        """Create a security group if it doesn't already exist."""
        if self._resource_exists(kubectl, "security group", sg_name):
            log.debug(f"[octavia] Security group '{sg_name}' already exists")
            return

        rc, out, err = self._run_openstack_cmd(
            kubectl,
            f"security group create {sg_name} -f json",
            retries=3,
        )
        if rc == 0:
            log.debug(f"[octavia] Security group '{sg_name}' created successfully")
        else:
            raise RuntimeError(
                f"[octavia] Failed to create security group '{sg_name}': {err or out}"
            )

    def _create_health_manager_security_group(self, kubectl):
        """Create health manager security group and rules."""
        sg_name = "lb-health-mgr-sec-grp"
        log.debug(f"[octavia] Ensuring health manager security group '{sg_name}'...")
        self._ensure_security_group(kubectl, sg_name)

        # Create rules: UDP 5555, 10514, 20514 and TCP 10514, 20514
        # SG rule creation is naturally idempotent (returns conflict if duplicate)
        rules = [
            ("udp", 5555),
            ("udp", 10514),
            ("udp", 20514),
            ("tcp", 10514),
            ("tcp", 20514),
        ]
        for proto, port in rules:
            rc, _, err = self._run_openstack_cmd(
                kubectl,
                (
                    f"security group rule create {sg_name} "
                    f"--ingress --ethertype IPv4 "
                    f"--protocol {proto} "
                    f"--dst-port {port}:{port}"
                ),
                retries=3,
            )
            if rc == 0:
                log.debug(f"[octavia] SG rule {proto}/{port} created for '{sg_name}'")
            else:
                # Rule likely already exists — not fatal
                log.debug(f"[octavia] SG rule {proto}/{port} already exists or skipped for '{sg_name}'")

        log.debug(f"[octavia] Health manager security group '{sg_name}' ready")

    def _create_amphora_security_group(self, kubectl):
        """Create amphora security group."""
        log.debug(f"[octavia] Ensuring amphora security group '{self.AMPHORA_SECURITY_GROUP_NAME}'...")
        self._ensure_security_group(kubectl, self.AMPHORA_SECURITY_GROUP_NAME)
        log.debug(f"[octavia] Amphora security group '{self.AMPHORA_SECURITY_GROUP_NAME}' ready")

    def _create_amphora_flavor(self, kubectl):
        """Create amphora compute flavor (m1.amphora), skip if exists."""
        name = self.AMPHORA_FLAVOR_NAME
        log.debug(f"[octavia] Ensuring amphora flavor '{name}'...")
        if self._resource_exists(kubectl, "flavor", name):
            log.debug(f"[octavia] Amphora flavor '{name}' already exists")
            return

        rc, out, err = self._run_openstack_cmd(
            kubectl,
            (
                f"flavor create {name} "
                f"--vcpus {self.AMPHORA_FLAVOR_VCPUS} "
                f"--ram {self.AMPHORA_FLAVOR_RAM} "
                f"--disk {self.AMPHORA_FLAVOR_DISK} "
                f"--private "
                f"-f json"
            ),
            retries=3,
        )
        if rc == 0:
            log.debug(f"[octavia] Amphora flavor '{name}' created successfully")
        else:
            raise RuntimeError(
                f"[octavia] Failed to create amphora flavor: {err or out}"
            )

    def _create_amphora_ssh_key(self, kubectl):
        """Create Amphora SSH key as a regular Kubernetes Secret.

        Generates an RSA key pair on the remote node via ssh-keygen,
        then stores it in a Secret (idempotent — skips if secret exists).
        """
        secret_name = f"{self.release_name}-amphora-ssh-key"
        log.debug("[octavia] Creating Amphora SSH key secret '%s'...", secret_name)

        # Check if secret already exists
        existing = kubectl.get_object(
            api_version="v1", kind="Secret",
            name=secret_name, namespace=self.namespace,
        )
        if existing:
            log.debug("[octavia] Amphora SSH key secret already exists, skipping")
            return

        # Generate key pair on remote node
        rc, out, err = kubectl.ssh.run(
            "tmp_dir=$(mktemp -d) && "
            'ssh-keygen -t rsa -b 4096 -N "" -f "$tmp_dir/id_rsa" -q && '
            'cat "$tmp_dir/id_rsa" | base64 -w0 && echo ":::SEP:::" && '
            'cat "$tmp_dir/id_rsa.pub" | base64 -w0 && '
            'rm -rf "$tmp_dir"',
            sudo=False,
        )
        if rc != 0:
            raise RuntimeError(f"[octavia] Failed to generate SSH key pair: {err}")

        parts = out.strip().split(":::SEP:::")
        if len(parts) != 2:
            raise RuntimeError("[octavia] Unexpected ssh-keygen output format")

        private_key_b64 = parts[0].strip()
        public_key_b64 = parts[1].strip()

        ssh_config = (
            "Host *\n"
            "  User ubuntu\n"
            "  StrictHostKeyChecking no\n"
            "  UserKnownHostsFile /dev/null\n"
        )

        secret_manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": secret_name,
                "namespace": self.namespace,
            },
            "type": "Opaque",
            "data": {
                "id_rsa": private_key_b64,
                "id_rsa.pub": public_key_b64,
                "config": base64.b64encode(ssh_config.encode()).decode(),
            },
        }
        try:
            kubectl.apply_objects([secret_manifest])
            log.debug("[octavia] Amphora SSH key secret created successfully")
        except Exception as e:
            raise RuntimeError(f"[octavia] Failed to create Amphora SSH key: {e}")

    # =================================================================
    # Pre-install: Create CAs & Issuers
    # (mirrors roles/octavia/tasks/main.yml "Create CAs & Issuers")
    # =================================================================
    def _create_cas_and_issuers(self, kubectl):
        """Create cert-manager CAs and Issuers for octavia-client and octavia-server."""
        log.debug("[octavia] Creating cert-manager CAs and Issuers...")

        for item in ("octavia-client", "octavia-server"):
            is_server = item == "octavia-server"
            common_name = (
                self.TLS_SERVER_COMMON_NAME if is_server
                else self.TLS_CLIENT_COMMON_NAME
            )
            algorithm = (
                self.TLS_SERVER_PRIVATE_KEY_ALGORITHM if is_server
                else self.TLS_CLIENT_PRIVATE_KEY_ALGORITHM
            )
            size = (
                self.TLS_SERVER_PRIVATE_KEY_SIZE if is_server
                else self.TLS_CLIENT_PRIVATE_KEY_SIZE
            )

            ca_cert = {
                "apiVersion": "cert-manager.io/v1",
                "kind": "Certificate",
                "metadata": {
                    "name": f"{item}-ca",
                    "namespace": self.namespace,
                },
                "spec": {
                    "isCA": True,
                    "commonName": common_name,
                    "secretName": f"{item}-ca",
                    "duration": "87600h0m0s",
                    "renewBefore": "720h0m0s",
                    "privateKey": {
                        "algorithm": algorithm,
                        "size": size,
                    },
                    "issuerRef": {
                        "name": "self-signed",
                        "kind": "ClusterIssuer",
                        "group": "cert-manager.io",
                    },
                },
            }

            issuer = {
                "apiVersion": "cert-manager.io/v1",
                "kind": "Issuer",
                "metadata": {
                    "name": item,
                    "namespace": self.namespace,
                },
                "spec": {
                    "ca": {
                        "secretName": f"{item}-ca",
                    },
                },
            }

            try:
                kubectl.apply_objects([ca_cert, issuer])
                log.debug(f"[octavia] CA and Issuer for '{item}' created successfully")
            except Exception as e:
                raise RuntimeError(
                    f"[octavia] Failed to create CA/Issuer for '{item}': {e}"
                )

        log.debug("[octavia] cert-manager CAs and Issuers created successfully")

    # =================================================================
    # Pre-install: Create Client Certificate
    # (mirrors "Create certificate for Octavia clients")
    # =================================================================
    def _create_client_certificate(self, kubectl):
        """Create cert-manager Certificate for Octavia client certs."""
        log.debug("[octavia] Creating client certificate 'octavia-client-certs'...")

        cert = {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {
                "name": "octavia-client-certs",
                "namespace": self.namespace,
            },
            "spec": {
                "commonName": self.TLS_CLIENT_COMMON_NAME,
                "secretName": "octavia-client-certs",
                "additionalOutputFormats": [
                    {"type": "CombinedPEM"},
                ],
                "duration": "87600h0m0s",
                "renewBefore": "720h0m0s",
                "privateKey": {
                    "algorithm": self.TLS_CLIENT_PRIVATE_KEY_ALGORITHM,
                    "size": self.TLS_CLIENT_PRIVATE_KEY_SIZE,
                },
                "issuerRef": {
                    "name": "octavia-client",
                    "kind": "Issuer",
                    "group": "cert-manager.io",
                },
            },
        }

        try:
            kubectl.apply_objects([cert])
            log.debug("[octavia] Client certificate 'octavia-client-certs' created successfully")
        except Exception as e:
            raise RuntimeError(
                f"[octavia] Failed to create client certificate: {e}"
            )

    # =================================================================
    # Pre-install: Create Admin Compute Quotaset
    # (mirrors "Create admin compute quotaset")
    # =================================================================
    def _create_admin_compute_quotaset(self, kubectl):
        """Set admin project quotas to unlimited (-1)."""
        log.debug("[octavia] Setting admin project compute quotas to unlimited...")

        quota_args = (
            "--instances -1 --cores -1 --ram -1 "
            "--volumes -1 --gigabytes -1 "
            "--secgroups -1 --secgroup-rules -1"
        )

        rc, out, err = self._run_openstack_cmd(
            kubectl,
            f"quota set admin {quota_args}",
            retries=5,
        )
        if rc == 0:
            log.debug("[octavia] Admin compute quotas set to unlimited successfully")
        else:
            log.debug(f"[octavia] WARNING: Failed to set admin quotas: {err or out}")

    # =================================================================
    # pre_install
    # =================================================================
    def pre_install(self, kubectl):
        log.info("[octavia] Ensuring RabbitMQ cluster...")
        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )
        rmq.ensure_cluster("octavia")

        log.info("[octavia] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="octavia",
        )

        log.info("[octavia] Reading keystone service password...")
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._octavia_keystone_password = secrets.require(
            "openstack_helm_endpoints_octavia_keystone_password"
        )

        log.debug(json.dumps(self._computed_endpoints, indent=2, sort_keys=True, default=str))

        log.info("[octavia] Generating OpenStack resources...")
        self._generate_resources(kubectl)

        log.info("[octavia] Creating cert-manager CAs and Issuers...")
        self._create_cas_and_issuers(kubectl)

        log.info("[octavia] Pre-install complete")

        # 6) Create client certificate
        self._create_client_certificate(kubectl)

        # 7) Create admin compute quotaset
        self._create_admin_compute_quotaset(kubectl)

        # 8) Clean up stale jobs to avoid upgrade conflicts
        self._cleanup_stale_jobs(kubectl)

        log.debug("[octavia] pre-install complete")

    def _cleanup_stale_jobs(self, kubectl):
        """Remove stale octavia jobs to avoid upgrade conflicts."""
        for job_name in ("octavia-db-sync", "octavia-rabbit-init"):
            rc, _, _ = kubectl._run(
                f"get job {job_name} -n {self.namespace} -o name"
            )
            if rc == 0:
                log.debug(f"[octavia] Deleting stale job {job_name}...")
                kubectl._run(f"delete job {job_name} -n {self.namespace}")

    # =================================================================
    # Post-install: Add Implied Roles
    # (mirrors "Add implied roles")
    # =================================================================
    def _add_implied_roles(self, kubectl):
        """Add implied roles for load-balancer (member -> load-balancer_member, etc.)."""
        log.debug("[octavia] Adding implied roles for load-balancer...")

        role_mappings = [
            {"role": "member", "implies": "load-balancer_member"},
            {"role": "reader", "implies": "load-balancer_observer"},
        ]

        for mapping in role_mappings:
            role = mapping["role"]
            implies = mapping["implies"]
            log.debug(f"[octavia] Creating implied role: {role} -> {implies}...")

            rc, out, err = self._run_openstack_cmd(
                kubectl,
                f"implied role create --implied-role {implies} {role}",
                retries=10,
                ignore_duplicate=True,
            )
            if rc == 0:
                log.debug(f"[octavia] Implied role {role} -> {implies} created successfully")
            elif "Duplicate entry" in (err or ""):
                log.debug(f"[octavia] Implied role {role} -> {implies} already exists")
            else:
                raise RuntimeError(
                    f"[octavia] Failed to create implied role {role} -> {implies}: {err or out}"
                )

        log.debug("[octavia] Implied roles added successfully")

    # =================================================================
    # post_install
    # =================================================================
    def post_install(self, kubectl):
        log.debug("[octavia] Starting post-install...")
        self.kubectl = kubectl

        # Parent handles Istio VirtualService + validation
        super().post_install(kubectl)

        self._wait_for_octavia_ready(kubectl)

        # Add implied roles (post helm deploy, requires keystone API)
        self._add_implied_roles(kubectl)

        log.debug("[octavia] post-install complete")

    def _wait_for_octavia_ready(self, kubectl):
        log.debug("[octavia] Waiting for octavia-api deployment...")
        kubectl.wait_for_deployment_ready(
            name="octavia-api",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[octavia] Octavia API ready")
