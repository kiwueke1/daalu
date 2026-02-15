# src/daalu/bootstrap/openstack/components/magnum/magnum.py

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


class MagnumComponent(InfraComponent):
    """
    Daalu Magnum component (OpenStack Container Infrastructure Management).

    Mirrors: roles/magnum/tasks/main.yml

    Pre-install:
    - Deploys Cluster API for Magnum RBAC (namespace + ClusterRoleBinding)
    - Ensures RabbitMQ cluster for magnum
    - Builds OpenStack endpoints (DB, RabbitMQ, Cache, Identity)
    - Reads keystone service password and stack user password

    Post-install:
    - Deploys magnum-cluster-api-proxy (ConfigMap + DaemonSet)
    - Creates Istio VirtualService for magnum-api (port 9511)
    - Deploys magnum-registry (Deployment + Service)
    - Creates Istio VirtualService for magnum-registry (port 5000)
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "magnum",
        secrets_path: Path,
        keystone_public_host: str,
        enable_argocd: bool = False,
        cluster_api_proxy_image: Optional[str] = None,
        registry_image: Optional[str] = None,
        network_backend: str = "ovn",
    ):
        # Derive the base domain from keystone_public_host for Istio
        parts = keystone_public_host.split(".")
        base_domain = ".".join(parts[1:]) if len(parts) > 1 else keystone_public_host

        super().__init__(
            name="magnum",
            repo_name="local",
            repo_url="",
            chart="magnum",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/magnum"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
            istio_enabled=True,
            istio_host=f"container-infra.{base_domain}",
            istio_service="magnum-api",
            istio_service_namespace=namespace,
            istio_service_port=9511,
            istio_expected_status=200,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self.secrets_path = secrets_path
        self.keystone_public_host = keystone_public_host
        self.wait_for_pods = True
        self.min_running_pods = 1
        self.cluster_api_proxy_image = cluster_api_proxy_image
        self.registry_image = registry_image
        self.network_backend = network_backend
        self._base_domain = base_domain

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

        region = "RegionOne"

        # Inject magnum service user auth into identity endpoint
        # Atmosphere convention: username = "magnum-<region>"
        endpoints["identity"]["auth"]["magnum"] = {
            "role": "admin",
            "region_name": region,
            "username": f"magnum-{region}",
            "password": self._magnum_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        # Inject magnum stack/domain user (trustee for cluster operations)
        # Atmosphere convention: same password, username = "magnum-domain-<region>"
        endpoints["identity"]["auth"]["magnum_stack_user"] = {
            "role": "admin",
            "region_name": region,
            "username": f"magnum-domain-{region}",
            "password": self._magnum_stack_user_keystone_password,
            "domain_name": "magnum",
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
    # Pre-install: Deploy Cluster API for Magnum RBAC
    # (mirrors "Deploy Cluster API for Magnum RBAC")
    # =================================================================
    def _deploy_cluster_api_rbac(self, kubectl):
        """Create magnum-system namespace and ClusterRoleBinding for magnum-conductor."""
        log.debug("[magnum] Deploying Cluster API for Magnum RBAC...")

        namespace_manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": "magnum-system",
            },
        }

        crb_manifest = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {
                "name": "magnum-cluster-api",
            },
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "cluster-admin",
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": "magnum-conductor",
                    "namespace": self.namespace,
                },
            ],
        }

        try:
            kubectl.apply_objects([namespace_manifest, crb_manifest])
            log.debug("[magnum] Cluster API RBAC deployed successfully")
        except Exception as e:
            raise RuntimeError(
                f"[magnum] Failed to deploy Cluster API RBAC: {e}"
            )

    # =================================================================
    # Post-install: Deploy magnum-cluster-api-proxy
    # (mirrors "Deploy magnum-cluster-api-proxy")
    # =================================================================
    def _deploy_cluster_api_proxy(self, kubectl):
        """Deploy magnum-cluster-api-proxy ConfigMap and DaemonSet."""
        if not self.cluster_api_proxy_image:
            log.debug("[magnum] Skipping cluster-api-proxy deployment (no image configured)")
            return

        log.debug("[magnum] Deploying magnum-cluster-api-proxy...")

        # Node selector depends on network backend
        if self.network_backend == "ovn":
            node_selector = {"openvswitch": "enabled"}
        else:
            node_selector = {"openstack-control-plane": "enabled"}

        configmap = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "magnum-cluster-api-proxy-config",
                "namespace": self.namespace,
            },
            "data": {
                "magnum_capi_sudoers": (
                    "Defaults !requiretty\n"
                    'Defaults secure_path="/usr/local/sbin:/usr/local/bin:'
                    "/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin:"
                    '/var/lib/openstack/bin:/var/lib/kolla/venv/bin"\n'
                    "magnum ALL = (root) NOPASSWD: "
                    "/var/lib/openstack/bin/privsep-helper\n"
                ),
            },
        }

        daemonset = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {
                "name": "magnum-cluster-api-proxy",
                "namespace": self.namespace,
                "labels": {
                    "application": "magnum",
                    "component": "cluster-api-proxy",
                },
            },
            "spec": {
                "selector": {
                    "matchLabels": {
                        "application": "magnum",
                        "component": "cluster-api-proxy",
                    },
                },
                "template": {
                    "metadata": {
                        "labels": {
                            "application": "magnum",
                            "component": "cluster-api-proxy",
                        },
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": "magnum-cluster-api-proxy",
                                "command": ["magnum-cluster-api-proxy"],
                                "image": self.cluster_api_proxy_image,
                                "securityContext": {
                                    "privileged": True,
                                    "readOnlyRootFilesystem": True,
                                },
                                "volumeMounts": [
                                    {"name": "pod-tmp", "mountPath": "/tmp"},
                                    {"name": "pod-run", "mountPath": "/run"},
                                    {
                                        "name": "config",
                                        "mountPath": "/etc/sudoers.d/magnum_capi_sudoers",
                                        "subPath": "magnum_capi_sudoers",
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "haproxy-state",
                                        "mountPath": "/var/lib/magnum/.magnum-cluster-api-proxy",
                                    },
                                    {
                                        "name": "host-run-netns",
                                        "mountPath": "/run/netns",
                                        "mountPropagation": "Bidirectional",
                                    },
                                ],
                            },
                        ],
                        "nodeSelector": node_selector,
                        "securityContext": {
                            "runAsUser": 42424,
                        },
                        "serviceAccountName": "magnum-conductor",
                        "volumes": [
                            {"name": "pod-tmp", "emptyDir": {}},
                            {"name": "pod-run", "emptyDir": {}},
                            {
                                "name": "config",
                                "configMap": {
                                    "name": "magnum-cluster-api-proxy-config",
                                },
                            },
                            {"name": "haproxy-state", "emptyDir": {}},
                            {
                                "name": "host-run-netns",
                                "hostPath": {"path": "/run/netns"},
                            },
                        ],
                    },
                },
            },
        }

        try:
            kubectl.apply_objects([configmap, daemonset])
            log.debug("[magnum] magnum-cluster-api-proxy deployed successfully")
        except Exception as e:
            raise RuntimeError(
                f"[magnum] Failed to deploy cluster-api-proxy: {e}"
            )

    # =================================================================
    # Post-install: Deploy magnum-registry
    # (mirrors "Deploy magnum registry")
    # =================================================================
    def _deploy_magnum_registry(self, kubectl):
        """Deploy magnum-registry Deployment and Service."""
        if not self.registry_image:
            log.debug("[magnum] Skipping magnum-registry deployment (no image configured)")
            return

        log.debug("[magnum] Deploying magnum-registry...")

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "magnum-registry",
                "namespace": self.namespace,
                "labels": {
                    "application": "magnum",
                    "component": "registry",
                },
            },
            "spec": {
                "replicas": 1,
                "selector": {
                    "matchLabels": {
                        "application": "magnum",
                        "component": "registry",
                    },
                },
                "template": {
                    "metadata": {
                        "labels": {
                            "application": "magnum",
                            "component": "registry",
                        },
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": "registry",
                                "image": self.registry_image,
                                "env": [
                                    {
                                        "name": "REGISTRY_STORAGE_MAINTENANCE_READONLY",
                                        "value": '{"enabled": true}',
                                    },
                                ],
                                "ports": [
                                    {
                                        "name": "registry",
                                        "containerPort": 5000,
                                        "protocol": "TCP",
                                    },
                                ],
                                "livenessProbe": {
                                    "httpGet": {
                                        "path": "/",
                                        "port": 5000,
                                        "scheme": "HTTP",
                                    },
                                },
                                "readinessProbe": {
                                    "httpGet": {
                                        "path": "/",
                                        "port": 5000,
                                        "scheme": "HTTP",
                                    },
                                },
                            },
                        ],
                        "nodeSelector": {
                            "openstack-control-plane": "enabled",
                        },
                    },
                },
            },
        }

        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": "magnum-registry",
                "namespace": self.namespace,
                "labels": {
                    "application": "magnum",
                    "component": "registry",
                },
            },
            "spec": {
                "type": "ClusterIP",
                "ports": [
                    {
                        "name": "magnum",
                        "port": 5000,
                        "protocol": "TCP",
                        "targetPort": 5000,
                    },
                ],
                "selector": {
                    "application": "magnum",
                    "component": "registry",
                },
            },
        }

        try:
            kubectl.apply_objects([deployment, service])
            log.debug("[magnum] magnum-registry deployed successfully")
        except Exception as e:
            raise RuntimeError(
                f"[magnum] Failed to deploy magnum-registry: {e}"
            )

    # =================================================================
    # Post-install: Create magnum-registry Istio VirtualService
    # (mirrors "Create magnum registry Ingress")
    # =================================================================
    def _create_registry_virtualservice(self, kubectl):
        """Create Istio VirtualService for magnum-registry (port 5000)."""
        if not self.registry_image:
            log.debug("[magnum] Skipping registry VirtualService (no registry deployed)")
            return

        vs_name = "magnum-registry-vs"
        registry_host = f"container-infra-registry.{self._base_domain}"

        log.debug(f"[magnum] Creating Istio VirtualService for magnum-registry ({registry_host})...")

        if kubectl.resource_exists(
            kind="virtualservice.networking.istio.io",
            name=vs_name,
            namespace=self.istio_namespace,
        ):
            log.debug(f"[magnum] Registry VirtualService '{vs_name}' already exists")
            return

        manifest = {
            "apiVersion": "networking.istio.io/v1beta1",
            "kind": "VirtualService",
            "metadata": {
                "name": vs_name,
                "namespace": self.istio_namespace,
            },
            "spec": {
                "gateways": [self.istio_gateway],
                "hosts": [registry_host],
                "http": [
                    {
                        "match": [{"uri": {"prefix": "/"}}],
                        "route": [
                            {
                                "destination": {
                                    "host": (
                                        f"magnum-registry."
                                        f"{self.namespace}."
                                        "svc.cluster.local"
                                    ),
                                    "port": {"number": 5000},
                                },
                            },
                        ],
                    },
                ],
            },
        }

        try:
            kubectl.apply_objects([manifest])
            log.debug(f"[magnum] Registry VirtualService '{vs_name}' created successfully")
        except Exception as e:
            raise RuntimeError(
                f"[magnum] Failed to create registry VirtualService: {e}"
            )

    # =================================================================
    # pre_install
    # =================================================================
    def pre_install(self, kubectl):
        log.debug("[magnum] Starting pre-install...")

        # 1) Deploy Cluster API for Magnum RBAC
        self._deploy_cluster_api_rbac(kubectl)

        # 2) Ensure RabbitMQ cluster for magnum
        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )
        rmq.ensure_cluster("magnum")

        # 3) Build OpenStack Helm endpoints (DB, Rabbit, Cache, Identity)
        log.debug("[magnum] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="magnum",
        )

        # 4) Read magnum keystone service passwords from secrets
        #    NOTE: Atmosphere uses the same password for both the magnum
        #    service user and the magnum_stack_user (domain admin).
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._magnum_keystone_password = secrets.require(
            "openstack_helm_endpoints_magnum_keystone_password"
        )
        self._magnum_stack_user_keystone_password = self._magnum_keystone_password
        log.debug("[magnum] OpenStack endpoints ready")

        log.debug("[magnum][DEBUG] Computed OpenStack Helm endpoints:")
        log.debug(json.dumps(self._computed_endpoints, indent=2, sort_keys=True, default=str))

        # 5) Clean up stale jobs to avoid upgrade conflicts
        self._cleanup_stale_jobs(kubectl)

        log.debug("[magnum] pre-install complete")

    def _cleanup_stale_jobs(self, kubectl):
        """Remove stale magnum jobs to avoid upgrade conflicts."""
        for job_name in ("magnum-db-sync", "magnum-rabbit-init"):
            rc, _, _ = kubectl._run(
                f"get job {job_name} -n {self.namespace} -o name"
            )
            if rc == 0:
                log.debug(f"[magnum] Deleting stale job {job_name}...")
                kubectl._run(f"delete job {job_name} -n {self.namespace}")

    # =================================================================
    # post_install
    # =================================================================
    def post_install(self, kubectl):
        log.debug("[magnum] Starting post-install...")
        self.kubectl = kubectl

        # Parent handles Istio VirtualService for magnum-api + validation
        super().post_install(kubectl)

        self._wait_for_magnum_ready(kubectl)

        # Deploy cluster-api-proxy DaemonSet (post helm, needs SA)
        self._deploy_cluster_api_proxy(kubectl)

        # Deploy magnum-registry Deployment + Service
        self._deploy_magnum_registry(kubectl)

        # Create Istio VirtualService for magnum-registry
        self._create_registry_virtualservice(kubectl)

        log.debug("[magnum] post-install complete")

    def _wait_for_magnum_ready(self, kubectl):
        log.debug("[magnum] Waiting for magnum-api deployment...")
        kubectl.wait_for_deployment_ready(
            name="magnum-api",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[magnum] Magnum API ready")
