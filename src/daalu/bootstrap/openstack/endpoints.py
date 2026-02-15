# src/daalu/bootstrap/openstack/endpoints.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Dict
import yaml

from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager
import logging

log = logging.getLogger("daalu")



@dataclass(frozen=True)
class OpenStackHelmEndpointsConfig:
    namespace: str = "openstack"
    region_name: str = "RegionOne"

    # Keystone public hostname for TLS public endpoint
    keystone_public_host: str = "keystone.example.com"

    # Services in-cluster:
    percona_haproxy_service: str = "percona-xtradb-haproxy"
    memcached_service: str = "memcached"

    # RabbitMQ: one cluster per service (Atmosphere style)
    rabbitmq_cluster_prefix: str = "rabbitmq-"  # -> rabbitmq-<svc>

    # For charts that still default to "mariadb" service dependency, we never use it.
    # We always point endpoints.oslo_db.hosts.default to percona HAProxy.
    db_port: int = 3306
    db_scheme: str = "mysql+pymysql"

    # Common chart knobs:
    cluster_domain_suffix: str = "cluster.local"


class OpenStackHelmEndpoints:
    """
    Atmosphere-like endpoints builder, but Daalu-native.

    Inputs:
      - SecretsManager (inventory secrets.yaml)
      - KubectlRunner (for operator-generated secrets)
      - Chart values.yaml (optional, to know which endpoints to build)
    """

    def __init__(
        self,
        *,
        cfg: OpenStackHelmEndpointsConfig,
        secrets: SecretsManager,
    ) -> None:
        self.cfg = cfg
        self.secrets = secrets

    # --------------------
    # Discovery: endpoints list
    # --------------------
    def chart_endpoints_keys(self, chart_values_yaml: Path, ignore: Optional[set[str]] = None) -> list[str]:
        ignore = ignore or set()
        data = yaml.safe_load(chart_values_yaml.read_text()) or {}
        endpoints = data.get("endpoints", {})
        if not isinstance(endpoints, dict):
            return []
        keys = [k for k in endpoints.keys() if k not in ignore]
        return keys

    # --------------------
    # Operators: read runtime secrets
    # --------------------
    def read_percona_root_password(self, kubectl) -> str:
        """
        Reads secret 'percona-xtradb' in openstack namespace (created by Percona operator).
        """
        sec = kubectl.get_object(api_version="v1", kind="Secret", name="percona-xtradb", namespace=self.cfg.namespace)
        if not sec:
            raise RuntimeError("Percona secret 'percona-xtradb' not found in namespace openstack")
        b64 = sec.get("data", {}).get("root")
        if not b64:
            raise RuntimeError("Percona secret 'percona-xtradb' missing data.root")
        return kubectl.b64decode_str(b64)

    def read_rabbitmq_user_password(self, kubectl, service: str) -> tuple[str, str]:
        """
        Reads secret 'rabbitmq-<service>-default-user' in openstack namespace.
        """
        name = f"rabbitmq-{service}-default-user"
        sec = kubectl.get_object(api_version="v1", kind="Secret", name=name, namespace=self.cfg.namespace)
        if not sec:
            raise RuntimeError(f"RabbitMQ secret '{name}' not found in namespace {self.cfg.namespace}")
        data = sec.get("data", {})
        u = data.get("username")
        p = data.get("password")
        if not u or not p:
            raise RuntimeError(f"RabbitMQ secret '{name}' missing username/password")
        return kubectl.b64decode_str(u), kubectl.b64decode_str(p)

    # --------------------
    # Build endpoints payloads
    # --------------------

    def build_common_endpoints(
        self,
        *,
        kubectl,
        service: str,
        keystone_api_service: str = "keystone-api",
    ) -> dict[str, Any]:
        """
        Returns an 'endpoints' dict suitable for chart values.
        It includes: identity, oslo_db, oslo_messaging, oslo_cache.
        """
        log.debug("starting build common endpoints")
        percona_root_pw = self.read_percona_root_password(kubectl)
        #rmq_user, rmq_pass = self.read_rabbitmq_user_password(kubectl, service)
        # -------------------------------------------------
        # RabbitMQ (one cluster per service)
        # -------------------------------------------------
        rabbitmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.cfg.namespace,
            replicas=1,
        )

        # 1) Ensure RabbitMQ cluster exists
        rabbitmq.ensure_cluster(service)

        # 2) Read operator-generated credentials
        rmq_user, rmq_pass = rabbitmq.get_default_user_credentials(service)

        #get barbican rabbit details
        svc_db_pw = self.secrets.service_db_passwords.get(service, "")
        svc_rabbit_pw = self.secrets.service_rabbit_passwords.get(service, "") or rmq_pass

        if not svc_db_pw:
            raise ValueError(
                f"Missing DB password for service '{service}'. "
                f"Add one of: {service}_database_password / {service}_db_password / {service}_mariadb_password / {service}_mysql_password to secrets.yaml"
            )

        memcache_key = self.secrets.get("openstack_helm_endpoints_memcached_secret_key", "")
        if not memcache_key:
            raise ValueError("Missing memcached_secret_key in secrets.yaml")

        endpoints: dict[str, Any] = {
            "cluster_domain_suffix": self.cfg.cluster_domain_suffix,

            # DB endpoint -> Percona HAProxy (NOT mariadb)
            "oslo_db": {
                "namespace": None,
                "auth": {
                    "admin": {
                        "username": "root",
                        "password": percona_root_pw,
                    },
                    service: {
                        "username": service,
                        "password": svc_db_pw,
                    },
                },
                "hosts": {"default": self.cfg.percona_haproxy_service},
                "path": f"/{service}",
                "scheme": self.cfg.db_scheme,
                "port": {"mysql": {"default": self.cfg.db_port}},
            },

            # RabbitMQ endpoint -> one cluster per service
            "oslo_messaging": {
                "namespace": None,
                "hosts": {"default": f"rabbitmq-{service}"},
                "path": f"/{service}",
                "scheme": "rabbit",
                "port": {
                    "amqp": {"default": 5672},
                    "http": {"default": 15672},
                },
                "auth": {
                    # Operator-generated user (used by rabbit-init + admin ops)
                    "admin": {
                        "username": rmq_user,
                        "password": rmq_pass,
                    },
                    "user": {
                        "username": rmq_user,
                        "password": rmq_pass,
                    },
                    # Service-level credentials used by OpenStack services
                    service: {
                        "username": service,
                        "password": svc_rabbit_pw,
                    },
                },
            },


            # Memcached (oslo.cache)
            "oslo_cache": {
                "namespace": None,
                "hosts": {"default": self.cfg.memcached_service},
                "auth": {"memcache_secret_key": memcache_key},
                "port": {"memcache": {"default": 11211}},
            },

            # Identity endpoint (Keystone itself)
            "identity": {
                "namespace": None,
                "name": "keystone",
                "auth": {
                    "admin": {
                        "region_name": self.cfg.region_name,
                        "username": f"admin-{self.cfg.region_name}",
                        "password": self.secrets.require("openstack_helm_endpoints_keystone_admin_password")
                        if self.secrets.get("openstack_helm_endpoints_keystone_admin_password")
                        else self.secrets.require("openstack_helm_endpoints_keystone_admin_password"),
                        "project_name": "admin",
                        "user_domain_name": "default",
                        "project_domain_name": "default",
                        "default_domain_id": "default",
                    }
                },
                "hosts": {"default": keystone_api_service, "internal": keystone_api_service},
                "scheme": {"default": "http", "service": "http", "public": "https"},
                "host_fqdn_override": {"public": {"host": self.cfg.keystone_public_host}},
                "path": {"default": "/"},
                "port": {"api": {"default": 5000, "public": 443}},
            },
        }

        return endpoints


