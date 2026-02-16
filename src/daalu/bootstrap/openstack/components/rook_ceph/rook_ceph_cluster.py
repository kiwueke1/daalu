# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/rook_ceph/rook_ceph_cluster.py

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Dict, Optional

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
import logging

log = logging.getLogger("daalu")


class RookCephClusterComponent(InfraComponent):
    """
    Daalu Rook-Ceph Cluster component.

    Connects an external Ceph cluster to Kubernetes via the Rook operator,
    deploys RadosGW with Keystone authentication, and registers Swift
    endpoints in the OpenStack catalog.

    Mirrors:
    roles/rook_ceph_cluster/tasks/main.yml
    roles/rook_ceph_cluster/defaults/main.yml
    roles/rook_ceph_cluster/vars/main.yml
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        cluster_name: str = "ceph",
        ssh,                              # SSHRunner connected to a Ceph monitor node
        secrets_path: Path,
        rgw_public_host: str,             # FQDN for public Swift endpoint
        ceph_image: str = "quay.io/ceph/ceph:v18.2.0",
        rgw_username: str = "rgw",
        region_name: str = "RegionOne",
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="rook-ceph-cluster",
            repo_name="local",
            repo_url="",
            chart="rook-ceph-cluster",
            version=None,
            namespace=namespace,
            release_name=cluster_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/rook-ceph-cluster"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
            # Istio for RadosGW exposure
            istio_enabled=True,
            istio_host=rgw_public_host,
            istio_service=f"rook-ceph-rgw-{cluster_name}",
            istio_service_namespace=namespace,
            istio_service_port=80,
            istio_expected_status=200,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self._ssh = ssh
        self.cluster_name = cluster_name
        self.secrets_path = secrets_path
        self.rgw_public_host = rgw_public_host
        self.ceph_image = ceph_image
        self.rgw_username = rgw_username
        self.region_name = region_name

        # Populated during pre_install
        self._rgw_password: Optional[str] = None
        self._admin_password: Optional[str] = None

        self.requires_public_ingress = False

        # User-supplied values from file
        self._user_values: Dict = {}
        if values_path and values_path.exists():
            self._user_values = self.load_values_file(values_path)

    # -------------------------------------------------
    # Ceph CLI helper (via SSH + cephadm on the Ceph node)
    # -------------------------------------------------
    CEPHADM_PATHS = "/usr/local/bin/cephadm:/usr/sbin/cephadm:/usr/bin/cephadm"

    def _run_ceph_cmd(self, ceph_args: str) -> tuple[int, str, str]:
        """
        Run a ceph CLI command via cephadm shell on the Ceph node.
        self._ssh must point to a node with cephadm installed.
        """
        # Resolve cephadm from known paths since sudo may strip PATH
        find_cephadm = (
            f"CEPHADM=$(for p in {self.CEPHADM_PATHS.replace(':', ' ')}; do "
            f"[ -x \"$p\" ] && echo \"$p\" && break; done) && "
            f"$CEPHADM shell -- ceph {ceph_args}"
        )
        return self._ssh.run(find_cephadm, sudo=True)

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        """
        1. Disable stray daemon warnings on Ceph cluster
        2. Collect quorum status, admin keyring, mon keyring
        3. Create rook-ceph-mon Secret with cluster credentials
        4. Create rook-ceph-mon-endpoints ConfigMap with leader monitor
        5. Load RGW and admin Keystone credentials from secrets
        """
        log.debug("[rook-ceph-cluster] Starting pre-install...")

        # --- Ceph monitor operations (via SSH to Ceph node) ---
        log.debug("[rook-ceph-cluster] Disabling stray daemon warnings...")
        self._run_ceph_cmd(
            "config set mgr mgr/cephadm/warn_on_stray_daemons false",
        )

        log.debug("[rook-ceph-cluster] Collecting Ceph quorum status...")
        rc, out, err = self._run_ceph_cmd(
            "quorum_status -f json",
        )
        if rc != 0:
            raise RuntimeError(f"Failed to get quorum status: {err}")
        quorum = json.loads(out)

        log.debug("[rook-ceph-cluster] Retrieving client.admin keyring...")
        rc, out, err = self._run_ceph_cmd(
            "auth get client.admin -f json",
        )
        if rc != 0:
            raise RuntimeError(f"Failed to get admin keyring: {err}")
        admin_auth = json.loads(out)[0]

        log.debug("[rook-ceph-cluster] Retrieving mon keyring...")
        rc, out, err = self._run_ceph_cmd(
            "auth get mon. -f json",
        )
        if rc != 0:
            raise RuntimeError(f"Failed to get mon keyring: {err}")
        mon_auth = json.loads(out)[0]

        # --- Extract cluster info ---
        fsid = quorum["monmap"]["fsid"]
        leader_name = quorum["quorum_leader_name"]
        leader_mon = next(
            m for m in quorum["monmap"]["mons"]
            if m["name"] == leader_name
        )
        leader_addr = leader_mon["public_addr"].split("/")[0]

        # --- Create Kubernetes resources ---
        log.debug("[rook-ceph-cluster] Creating rook-ceph-mon Secret...")
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "rook-ceph-mon",
                    "namespace": self.namespace,
                },
                "stringData": {
                    "cluster-name": self.cluster_name,
                    "fsid": fsid,
                    "admin-secret": admin_auth["key"],
                    "mon-secret": mon_auth["key"],
                },
            },
        ])

        log.debug("[rook-ceph-cluster] Creating rook-ceph-mon-endpoints ConfigMap...")
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "rook-ceph-mon-endpoints",
                    "namespace": self.namespace,
                },
                "data": {
                    "data": f"{leader_name}={leader_addr}",
                    "maxMonId": "0",
                    "mapping": "{}",
                },
            },
        ])

        # --- Load credentials from secrets.yaml ---
        log.debug("[rook-ceph-cluster] Loading credentials from secrets...")
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._rgw_password = secrets.require(
            "openstack_helm_endpoints_rgw_keystone_password"
        )
        self._admin_password = secrets.require(
            "openstack_helm_endpoints_keystone_admin_password"
        )

        log.debug("[rook-ceph-cluster] pre-install complete")

    # -------------------------------------------------
    # values
    # -------------------------------------------------
    def values(self) -> Dict:
        """
        Load base values from file, then inject dynamic fields
        (clusterName, configOverride, ceph image).
        """
        if self._rgw_password is None:
            raise RuntimeError("pre_install must run before values()")

        base = self.load_values_file(self.values_path)

        # Inject dynamic values that depend on runtime config / secrets
        base["clusterName"] = self.cluster_name

        base.setdefault("cephClusterSpec", {})
        base["cephClusterSpec"]["cephVersion"] = {"image": self.ceph_image}

        # Override object store name with cluster_name
        for store in base.get("cephObjectStores", []):
            store["name"] = self.cluster_name

        # RGW <-> Keystone auth config (contains secrets, must stay in code)
        base["configOverride"] = (
            "[client]\n"
            "rgw keystone api version = 3\n"
            "rgw keystone url = http://keystone-api.openstack.svc.cluster.local:5000\n"
            f"rgw keystone admin user = {self.rgw_username}\n"
            f"rgw keystone admin password = {self._rgw_password}\n"
            "rgw_keystone admin domain = service\n"
            "rgw_keystone admin project = service\n"
            "rgw keystone implicit tenants = true\n"
            "rgw keystone accepted roles = member,admin,reader\n"
            "rgw_keystone accepted admin roles = admin\n"
            "rgw keystone token cache size = 0\n"
            "rgw s3 auth use keystone = true\n"
            "rgw swift account in url = true\n"
            "rgw swift versioning enabled = true\n"
        )

        return base

    # -------------------------------------------------
    # post_install steps
    # -------------------------------------------------
    def _build_env_prefix(self) -> str:
        openrc = self._build_openrc_env()
        return " ".join(f"{k}={shlex.quote(v)}" for k, v in openrc.items())

    def _ensure_service_domain(self, kubectl, pod, env):
        log.debug("[rook-ceph-cluster] Ensuring 'service' domain...")
        self._ks_run(
            kubectl, pod, env,
            "openstack domain create service || true"
        )

    def _ensure_rgw_user(self, kubectl, pod, env):
        log.debug(f"[rook-ceph-cluster] Ensuring user '{self.rgw_username}'...")
        self._ks_run(
            kubectl, pod, env,
            f"openstack user create "
            f"--domain service "
            f"--password {shlex.quote(self._rgw_password)} "
            f"{self.rgw_username} || true"
        )

    def _ensure_service_project(self, kubectl, pod, env):
        log.debug("[rook-ceph-cluster] Ensuring 'service' project...")
        self._ks_run(
            kubectl, pod, env,
            "openstack project create service "
            "--domain service || true"
        )

    def _grant_rgw_admin_role(self, kubectl, pod, env):
        log.debug("[rook-ceph-cluster] Granting admin role to RGW user...")
        rc, out, err = self._ks_run(
            kubectl, pod, env,
            f"openstack role add "
            f"--user-domain service "
            f"--project service "
            f"--user {self.rgw_username} "
            f"admin"
        )
        if rc != 0 and "Conflict" not in (err or ""):
            raise RuntimeError(f"Failed to grant role: {err or out}")

    def _ensure_swift_service(self, kubectl, pod, env):
        log.debug("[rook-ceph-cluster] Ensuring 'swift' service exists...")

        # Check first (Ansible-style)
        rc, out, err = self._ks_run(
            kubectl, pod, env,
            "openstack service list "
            "-f value -c Type | grep -w object-store"
        )

        if rc == 0 and out.strip():
            log.debug("[rook-ceph-cluster] Swift service already exists")
            return

        log.debug("[rook-ceph-cluster] Creating Swift service...")
        rc, out, err = self._ks_run(
            kubectl, pod, env,
            "openstack service create "
            "--name swift "
            "object-store"
        )

        log.debug("[rook-ceph-cluster] Swift service create output:")
        log.debug(out.strip())

        if rc != 0:
            raise RuntimeError(f"Failed to create swift service: {err or out}")

    def _ensure_swift_endpoints(self, kubectl, pod, env):
        internal_url = (
            f"http://rook-ceph-rgw-{self.cluster_name}"
            f".{self.namespace}.svc.cluster.local"
            f"/swift/v1/%(tenant_id)s"
        )
        public_url = (
            f"https://{self.rgw_public_host}"
            f"/swift/v1/%(tenant_id)s"
        )

        self._ensure_endpoint(kubectl, pod, env, "public", public_url)
        self._ensure_endpoint(kubectl, pod, env, "internal", internal_url)


    def post_install(self, kubectl):
        """
        Post-install orchestration only.
        """
        log.debug("[rook-ceph-cluster] Starting post-install...")
        self.kubectl = kubectl

        super().post_install(kubectl)

        pod = self._get_keystone_api_pod()
        env_prefix = self._build_env_prefix()

        self._ensure_service_domain(kubectl, pod, env_prefix)
        self._ensure_rgw_user(kubectl, pod, env_prefix)
        self._ensure_service_project(kubectl, pod, env_prefix)
        self._grant_rgw_admin_role(kubectl, pod, env_prefix)
        self._ensure_swift_service(kubectl, pod, env_prefix)
        self._ensure_swift_endpoints(kubectl, pod, env_prefix)

        log.debug("[rook-ceph-cluster] post-install complete")


    def post_install_1(self, kubectl):
        """
        1. Ensure 'service' domain in Keystone
        2. Create RGW user in Keystone
        3. Ensure 'service' project
        4. Grant admin role to RGW user on service project
        5. Create 'swift' service in catalog
        6. Create Swift public and internal endpoints
        """
        log.debug("[rook-ceph-cluster] Starting post-install...")
        self.kubectl = kubectl

        # Parent handles Istio VirtualService + validation
        super().post_install(kubectl)

        pod = self._get_keystone_api_pod()
        openrc = self._build_openrc_env()
        env_prefix = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in openrc.items()
        )

        # 1. Ensure 'service' domain
        log.debug("[rook-ceph-cluster] Ensuring 'service' domain...")
        self._ks_run(kubectl, pod, env_prefix,
            "openstack domain create --or-show service "
        )

        # 2. Create RGW user
        log.debug(f"[rook-ceph-cluster] Creating user '{self.rgw_username}'...")
        self._ks_run(kubectl, pod, env_prefix,
            f"openstack user create --or-show "
            f"--domain service "
            f"--password {shlex.quote(self._rgw_password)} "
            f"{self.rgw_username}"
        )

        # 3. Ensure 'service' project
        log.debug("[rook-ceph-cluster] Ensuring 'service' project...")
        self._ks_run(kubectl, pod, env_prefix,
            "openstack project create --or-show "
            "--domain service "
            "service"
        )

        # 4. Grant admin role
        log.debug("[rook-ceph-cluster] Granting admin role to RGW user...")
        cmd = (
            f"openstack role add "
            f"--user-domain service "
            f"--project service "
            f"--user {self.rgw_username} "
            f"admin"
        )
        rc, out, err = self._ks_run(kubectl, pod, env_prefix, cmd)
        # role add fails if already assigned — that's OK
        if rc != 0 and "Conflict" not in (err or ""):
            log.debug(f"[rook-ceph-cluster] Warning: role add returned rc={rc}: {err}")

        # 5. Create swift service
        log.debug("[rook-ceph-cluster] Creating 'swift' service...")
        self._ks_run(kubectl, pod, env_prefix,
            "openstack service create --or-show "
            "--name swift "
            "object-store"
        )

        # 6. Create Swift endpoints
        internal_url = (
            f"http://rook-ceph-rgw-{self.cluster_name}"
            f".{self.namespace}.svc.cluster.local"
            f"/swift/v1/%(tenant_id)s"
        )
        public_url = (
            f"https://{self.rgw_public_host}"
            f"/swift/v1/%(tenant_id)s"
        )

        self._ensure_endpoint(kubectl, pod, env_prefix, "public", public_url)
        self._ensure_endpoint(kubectl, pod, env_prefix, "internal", internal_url)

        log.debug("[rook-ceph-cluster] post-install complete")

    # -------------------------------------------------
    # Private helpers
    # -------------------------------------------------

    def _ks_run(
        self, kubectl, pod: str, env_prefix: str, cmd: str,
    ) -> tuple[int, str, str]:
        """Execute an openstack CLI command inside the keystone-api pod."""
        full_cmd = (
            f"exec {pod} -n {self.namespace} -c keystone-api -- "
            f"env {env_prefix} {cmd}"
        )
        return kubectl._run(full_cmd)

    def _ensure_endpoint(
        self, kubectl, pod: str, env_prefix: str,
        interface: str, url: str,
    ):
        """Create a Swift endpoint only if one doesn't already exist."""
        log.debug(f"[rook-ceph-cluster] Ensuring {interface} endpoint...")
        check_cmd = (
            f"openstack endpoint list "
            f"--service object-store "
            f"--interface {interface} "
            f"--region {self.region_name} "
            f"-f value -c ID"
        )
        rc, out, err = self._ks_run(kubectl, pod, env_prefix, check_cmd)
        if rc == 0 and out.strip():
            log.debug(f"[rook-ceph-cluster] {interface} endpoint already exists")
            return

        create_cmd = (
            f"openstack endpoint create "
            f"--region {self.region_name} "
            f"object-store {interface} "
            f'"{url}"'
        )
        log.debug(f"endpoint create_cmd is: {create_cmd}")
        rc, out, err = self._ks_run(kubectl, pod, env_prefix, create_cmd)
        if rc != 0:
            raise RuntimeError(
                f"Failed to create {interface} endpoint: {err or out}"
            )
        log.debug(f"[rook-ceph-cluster] {interface} endpoint created")

    def _get_keystone_api_pod(self) -> str:
        """Find a running keystone-api pod."""
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
        """Build OpenRC environment for admin auth against keystone-api."""
        env = {
            "OS_IDENTITY_API_VERSION": "3",
            "OS_AUTH_URL": "http://keystone-api:5000/v3",
            "OS_REGION_NAME": self.region_name,
            "OS_INTERFACE": "internal",
            "OS_PROJECT_DOMAIN_NAME": "default",
            "OS_PROJECT_NAME": "admin",
            "OS_USER_DOMAIN_NAME": "default",
            "OS_USERNAME": f"admin-{self.region_name}",
            "OS_PASSWORD": self._admin_password,
            "OS_DEFAULT_DOMAIN": "default",
        }

        log.debug("\n# ===== OpenRC (admin) =====")
        for k, v in env.items():
            log.debug(f"export {k}={v}")
        log.debug("# =========================\n")

        return env


    @staticmethod
    def _deep_merge(a: dict, b: dict) -> dict:
        """Recursively merge *b* into *a*; *b* wins on conflicts."""
        out = dict(a)
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = RookCephClusterComponent._deep_merge(out[k], v)
            else:
                out[k] = v
        return out