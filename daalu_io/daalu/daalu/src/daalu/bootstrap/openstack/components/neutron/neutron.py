# src/daalu/bootstrap/openstack/components/neutron/neutron.py

from __future__ import annotations

from pathlib import Path
import json

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.engine.values import deep_merge
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.utils.helpers import build_openstack_endpoints
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager
import logging

log = logging.getLogger("daalu")


class NeutronComponent(InfraComponent):
    """
    Daalu Neutron component (OpenStack Networking).

    Deploys the Neutron Helm chart providing:
    - neutron-server (Network API)
    - ML2 plugin with OVN or OVS backend
    - OVN metadata agent (when OVN backend)
    - OVN VPN agent (when OVN backend)
    - DHCP / L3 / metadata agents (when non-OVN backend)
    - DB sync and rabbit-init jobs

    Pre-install:
    - Ensures RabbitMQ cluster for neutron
    - Builds OpenStack endpoints (DB, RabbitMQ, Cache, Identity)
    - Reads keystone service password and metadata shared secret
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "neutron",
        secrets_path: Path,
        keystone_public_host: str,
        network_backend: str = "ovn",
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="neutron",
            repo_name="local",
            repo_url="",
            chart="neutron",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/neutron"),
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
        self.network_backend = network_backend
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

        # Inject neutron service user auth into identity endpoint
        endpoints["identity"]["auth"]["neutron"] = {
            "role": "admin,service",
            "region_name": "RegionOne",
            "username": "neutron",
            "password": self._neutron_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        # Inject nova service user auth (neutron [nova] section for vif-plugged notifications)
        endpoints["identity"]["auth"]["nova"] = {
            "region_name": "RegionOne",
            "username": "nova",
            "password": self._nova_keystone_password,
            "project_name": "service",
            "user_domain_name": "service",
            "project_domain_name": "service",
        }

        base["endpoints"] = endpoints

        # Set network backend
        base.setdefault("network", {})
        base["network"]["backend"] = [self.network_backend]

        # Inject metadata proxy shared secret
        if self._metadata_secret:
            base.setdefault("conf", {})
            base["conf"].setdefault("metadata_agent", {})
            base["conf"]["metadata_agent"].setdefault("DEFAULT", {})
            base["conf"]["metadata_agent"]["DEFAULT"]["metadata_proxy_shared_secret"] = (
                self._metadata_secret
            )

        # Apply OVN-specific overrides
        if self.network_backend == "ovn":
            base = self._apply_ovn_overrides(base)

        return base

    def _apply_ovn_overrides(self, base: dict) -> dict:
        """Apply OVN-specific configuration overrides."""
        ovn_overrides = {
            "conf": {
                "neutron": {
                    "DEFAULT": {
                        "service_plugins": "qos,ovn-router,segments,trunk,log,ovn-vpnaas",
                    },
                    "ovn": {
                        "ovn_emit_need_to_frag": True,
                    },
                    "service_providers": {
                        "service_provider": (
                            "VPN:strongswan:neutron_vpnaas.services.vpn.service_drivers"
                            ".ovn_ipsec.IPsecOvnVPNDriver:default"
                        ),
                    },
                },
                "ovn_metadata_agent": {
                    "DEFAULT": {
                        "metadata_proxy_shared_secret": self._metadata_secret,
                    },
                },
                "ovn_vpn_agent": {
                    "AGENT": {
                        "extensions": "vpnaas",
                    },
                    "vpnagent": {
                        "vpn_device_driver": (
                            "neutron_vpnaas.services.vpn.device_drivers"
                            ".ovn_ipsec.OvnStrongSwanDriver"
                        ),
                    },
                },
                "neutron_vpnaas": {
                    "service_providers": {
                        "service_provider": (
                            "VPN:strongswan:neutron_vpnaas.services.vpn.service_drivers"
                            ".ovn_ipsec.IPsecOvnVPNDriver:default"
                        ),
                    },
                },
                "plugins": {
                    "ml2_conf": {
                        "agent": {
                            "extensions": "log",
                        },
                        "ml2": {
                            "type_drivers": "flat,vlan,geneve",
                            "tenant_network_types": "geneve",
                        },
                    },
                },
            },
            "manifests": {
                "daemonset_dhcp_agent": False,
                "daemonset_l3_agent": False,
                "daemonset_metadata_agent": False,
                "daemonset_ovn_metadata_agent": True,
                "daemonset_ovn_vpn_agent": True,
                "daemonset_ovs_agent": False,
                "deployment_rpc_server": False,
            },
        }

        return deep_merge(base, ovn_overrides)

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        log.debug("[neutron] Starting pre-install...")

        # 1) Ensure RabbitMQ cluster for neutron
        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )
        rmq.ensure_cluster("neutron")

        # 2) Build OpenStack Helm endpoints (DB, Rabbit, Cache, Identity)
        log.debug("[neutron] Building OpenStack Helm endpoints...")
        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=self.keystone_public_host,
            service="neutron",
        )

        # 3) Read neutron keystone service password from secrets
        secrets = SecretsManager.from_yaml(
            path=self.secrets_path,
            namespace=self.namespace,
        )
        self._neutron_keystone_password = secrets.require(
            "openstack_helm_endpoints_neutron_keystone_password"
        )

        # 4) Read nova keystone service password (neutron [nova] section for notifications)
        self._nova_keystone_password = secrets.require(
            "openstack_helm_endpoints_nova_keystone_password"
        )

        # 5) Read metadata proxy shared secret
        self._metadata_secret = secrets.get(
            "openstack_helm_endpoints_compute_metadata_secret",
            "",
        )

        log.debug("[neutron] OpenStack endpoints ready")

        log.debug("[neutron][DEBUG] Computed OpenStack Helm endpoints:")
        log.debug(json.dumps(self._computed_endpoints, indent=2, sort_keys=True, default=str))

        # 5) Clean up stale jobs to avoid upgrade conflicts
        self._cleanup_stale_jobs(kubectl)

        log.debug("[neutron] pre-install complete")

    def _cleanup_stale_jobs(self, kubectl):
        """Remove stale neutron jobs to avoid upgrade conflicts."""
        for job_name in ("neutron-db-sync", "neutron-rabbit-init"):
            rc, _, _ = kubectl._run(
                f"get job {job_name} -n {self.namespace} -o name"
            )
            if rc == 0:
                log.debug(f"[neutron] Deleting stale job {job_name}...")
                kubectl._run(f"delete job {job_name} -n {self.namespace}")

    # -------------------------------------------------
    # post_install
    # -------------------------------------------------
    def post_install(self, kubectl):
        log.debug("[neutron] Starting post-install...")
        self.kubectl = kubectl

        super().post_install(kubectl)

        self._wait_for_neutron_ready(kubectl)

        log.debug("[neutron] post-install complete")

    def _wait_for_neutron_ready(self, kubectl):
        log.debug("[neutron] Waiting for neutron-server deployment...")
        kubectl.wait_for_deployment_ready(
            name="neutron-server",
            namespace=self.namespace,
            timeout=600,
        )
        log.debug("[neutron] Neutron server ready")
