# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/openstack_exporter/openstack_exporter.py

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Optional

from daalu.bootstrap.engine.component import InfraComponent
import logging

log = logging.getLogger("daalu")


# Default images (matching Atmosphere defaults)
DEFAULT_BOOTSTRAP_IMAGE = "docker.io/openstackhelm/heat:2024.1-ubuntu_jammy"
DEFAULT_EXPORTER_IMAGE = "ghcr.io/openstack-exporter/openstack-exporter:latest"
DEFAULT_DB_EXPORTER_IMAGE = "ghcr.io/vexxhost/openstack-database-exporter:latest"


class OpenStackExporterComponent(InfraComponent):
    """
    Daalu OpenStack Exporter component (Prometheus metrics exporters).

    Mirrors: roles/openstack_exporter/tasks/main.yml

    This is NOT a Helm chart. It deploys raw K8s manifests:

    1) openstack-exporter Deployment + Service
       - Reads admin credentials from keystone-keystone-admin Secret
       - Exposes Prometheus metrics on port 9180

    2) openstack-database-exporter Deployment + Service
       - Fetches DB connection strings from neutron-db-user,
         nova-db-user, octavia-db-user Secrets
       - Creates openstack-database-exporter-dsn Secret with DSNs
       - Exposes Prometheus metrics on port 9180
    """

    def __init__(
        self,
        *,
        kubeconfig: str,
        namespace: str = "openstack",
        bootstrap_image: Optional[str] = None,
        exporter_image: Optional[str] = None,
        db_exporter_image: Optional[str] = None,
    ):
        super().__init__(
            name="openstack-exporter",
            repo_name="local",
            repo_url="",
            chart="",
            version=None,
            namespace=namespace,
            release_name="openstack-exporter",
            local_chart_dir=None,
            remote_chart_dir=None,
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self._bootstrap_image = bootstrap_image or DEFAULT_BOOTSTRAP_IMAGE
        self._exporter_image = exporter_image or DEFAULT_EXPORTER_IMAGE
        self._db_exporter_image = db_exporter_image or DEFAULT_DB_EXPORTER_IMAGE
        self.wait_for_pods = False
        self.min_running_pods = 0
        self.enable_argocd = False

    # =================================================================
    # Deploy openstack-exporter (Deployment + Service)
    # (mirrors first "Deploy service" task)
    # =================================================================
    def _deploy_openstack_exporter(self, kubectl):
        """Deploy openstack-exporter Deployment and headless Service."""
        log.debug("[openstack-exporter] Deploying openstack-exporter...")

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "openstack-exporter",
                "namespace": self.namespace,
                "labels": {
                    "application": "openstack-exporter",
                },
            },
            "spec": {
                "replicas": 1,
                "selector": {
                    "matchLabels": {
                        "application": "openstack-exporter",
                    },
                },
                "template": {
                    "metadata": {
                        "labels": {
                            "application": "openstack-exporter",
                        },
                    },
                    "spec": {
                        "nodeSelector": {
                            "openstack-control-plane": "enabled",
                        },
                        "initContainers": [
                            {
                                "name": "build-config",
                                "image": self._bootstrap_image,
                                "command": ["bash", "-ec"],
                                "args": [
                                    (
                                        'cat <<EOF > /etc/openstack/clouds.yaml\n'
                                        '---\n'
                                        'clouds:\n'
                                        '  default:\n'
                                        '    auth:\n'
                                        '      auth_url: "$(OS_AUTH_URL)"\n'
                                        '      project_domain_name: "$(OS_PROJECT_DOMAIN_NAME)"\n'
                                        '      project_name: "$(OS_PROJECT_NAME)"\n'
                                        '      user_domain_name: "$(OS_USER_DOMAIN_NAME)"\n'
                                        '      username: "$(OS_USERNAME)"\n'
                                        '      password: "$(OS_PASSWORD)"\n'
                                        '    region_name: "$(OS_REGION_NAME)"\n'
                                        '    interface: "$(OS_INTERFACE)"\n'
                                        '    identity_api_version: 3\n'
                                        '    identity_interface: "$(OS_INTERFACE)"\n'
                                        'EOF'
                                    ),
                                ],
                                "envFrom": [
                                    {
                                        "secretRef": {
                                            "name": "keystone-keystone-admin",
                                        },
                                    },
                                ],
                                "volumeMounts": [
                                    {
                                        "name": "openstack-config",
                                        "mountPath": "/etc/openstack",
                                    },
                                ],
                            },
                        ],
                        "containers": [
                            {
                                "name": "openstack-exporter",
                                "image": self._exporter_image,
                                "args": [
                                    "--endpoint-type",
                                    "internal",
                                    "default",
                                    "--collect-metric-time",
                                    "-d", "neutron-l3_agent_of_router",
                                    "--disable-service.load-balancer",
                                ],
                                "ports": [
                                    {
                                        "name": "metrics",
                                        "containerPort": 9180,
                                    },
                                ],
                                "env": [
                                    {
                                        "name": "OS_COMPUTE_API_VERSION",
                                        "value": "2.87",
                                    },
                                ],
                                "volumeMounts": [
                                    {
                                        "name": "openstack-config",
                                        "mountPath": "/etc/openstack",
                                    },
                                ],
                                "readinessProbe": {
                                    "failureThreshold": 3,
                                    "httpGet": {
                                        "path": "/",
                                        "port": 9180,
                                        "scheme": "HTTP",
                                    },
                                },
                                "livenessProbe": {
                                    "failureThreshold": 3,
                                    "httpGet": {
                                        "path": "/",
                                        "port": 9180,
                                        "scheme": "HTTP",
                                    },
                                    "periodSeconds": 10,
                                    "successThreshold": 1,
                                    "timeoutSeconds": 1,
                                },
                            },
                        ],
                        "volumes": [
                            {
                                "name": "openstack-config",
                                "emptyDir": {},
                            },
                        ],
                    },
                },
            },
        }

        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": "openstack-exporter",
                "namespace": self.namespace,
                "labels": {
                    "application": "openstack-exporter",
                },
            },
            "spec": {
                "clusterIP": "None",
                "ports": [
                    {
                        "name": "metrics",
                        "port": 9180,
                        "targetPort": "metrics",
                    },
                ],
                "selector": {
                    "application": "openstack-exporter",
                },
            },
        }

        try:
            kubectl.apply_objects([deployment, service])
            log.debug("[openstack-exporter] openstack-exporter Deployment and Service created successfully")
        except Exception as e:
            raise RuntimeError(
                f"[openstack-exporter] Failed to deploy openstack-exporter: {e}"
            )

    # =================================================================
    # Fetch DB secrets and create DSN secret
    # (mirrors "Fetch Neutron/Nova/Octavia DB secret" +
    #  "Create openstack-database-exporter-dsn secret")
    # =================================================================
    def _fetch_db_secret(self, kubectl, secret_name: str) -> Optional[str]:
        """Fetch DB_CONNECTION from a K8s Secret, return decoded value or None."""
        rc, out, _ = kubectl._run(
            f"get secret {secret_name} -n {self.namespace} "
            f"-o jsonpath={{.data.DB_CONNECTION}}"
        )
        if rc != 0 or not out.strip():
            log.debug(f"[openstack-exporter] WARNING: Secret '{secret_name}' not found or empty")
            return None

        try:
            decoded = base64.b64decode(out.strip()).decode("utf-8")
            return decoded
        except Exception as e:
            log.debug(f"[openstack-exporter] WARNING: Failed to decode '{secret_name}': {e}")
            return None

    def _create_dsn_secret(self, kubectl):
        """Fetch DB secrets and create the openstack-database-exporter-dsn Secret."""
        log.debug("[openstack-exporter] Fetching database secrets...")

        neutron_dsn = self._fetch_db_secret(kubectl, "neutron-db-user")
        nova_dsn = self._fetch_db_secret(kubectl, "nova-db-user")
        octavia_dsn = self._fetch_db_secret(kubectl, "octavia-db-user")

        if not all([neutron_dsn, nova_dsn, octavia_dsn]):
            log.debug(
                "[openstack-exporter] WARNING: Some DB secrets are missing. "
                "Database exporter may not function correctly."
            )

        # Strip +pymysql from DSNs (matching Ansible replace('+pymysql', ''))
        def clean_dsn(dsn: Optional[str]) -> str:
            if not dsn:
                return ""
            return dsn.replace("+pymysql", "")

        neutron_url = clean_dsn(neutron_dsn)
        nova_url = clean_dsn(nova_dsn)
        octavia_url = clean_dsn(octavia_dsn)

        log.debug(f"[openstack-exporter] NEUTRON_DATABASE_URL: {neutron_url[:50]}...")
        log.debug(f"[openstack-exporter] NOVA_DATABASE_URL: {nova_url[:50]}...")
        log.debug(f"[openstack-exporter] OCTAVIA_DATABASE_URL: {octavia_url[:50]}...")

        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": "openstack-database-exporter-dsn",
                "namespace": self.namespace,
                "labels": {
                    "application": "openstack-database-exporter",
                },
            },
            "stringData": {
                "NEUTRON_DATABASE_URL": neutron_url,
                "NOVA_DATABASE_URL": nova_url,
                "NOVA_API_DATABASE_URL": f"{nova_url}_api",
                "OCTAVIA_DATABASE_URL": octavia_url,
            },
        }

        try:
            kubectl.apply_objects([secret])
            log.debug("[openstack-exporter] openstack-database-exporter-dsn Secret created successfully")
        except Exception as e:
            raise RuntimeError(
                f"[openstack-exporter] Failed to create DSN secret: {e}"
            )

    # =================================================================
    # Deploy openstack-database-exporter (Deployment)
    # (mirrors second "Deploy service" task)
    # =================================================================
    def _deploy_database_exporter(self, kubectl):
        """Deploy openstack-database-exporter Deployment."""
        log.debug("[openstack-exporter] Deploying openstack-database-exporter...")

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "openstack-database-exporter",
                "namespace": self.namespace,
                "labels": {
                    "application": "openstack-database-exporter",
                },
            },
            "spec": {
                "replicas": 1,
                "selector": {
                    "matchLabels": {
                        "application": "openstack-database-exporter",
                    },
                },
                "template": {
                    "metadata": {
                        "labels": {
                            "application": "openstack-database-exporter",
                        },
                    },
                    "spec": {
                        "nodeSelector": {
                            "openstack-control-plane": "enabled",
                        },
                        "containers": [
                            {
                                "name": "openstack-database-exporter",
                                "image": self._db_exporter_image,
                                "envFrom": [
                                    {
                                        "secretRef": {
                                            "name": "openstack-database-exporter-dsn",
                                        },
                                    },
                                ],
                                "ports": [
                                    {
                                        "name": "metrics",
                                        "containerPort": 9180,
                                    },
                                ],
                                "readinessProbe": {
                                    "failureThreshold": 3,
                                    "httpGet": {
                                        "path": "/",
                                        "port": 9180,
                                        "scheme": "HTTP",
                                    },
                                },
                                "livenessProbe": {
                                    "failureThreshold": 3,
                                    "httpGet": {
                                        "path": "/",
                                        "port": 9180,
                                        "scheme": "HTTP",
                                    },
                                    "periodSeconds": 10,
                                    "successThreshold": 1,
                                    "timeoutSeconds": 1,
                                },
                            },
                        ],
                    },
                },
            },
        }

        try:
            kubectl.apply_objects([deployment])
            log.debug("[openstack-exporter] openstack-database-exporter Deployment created successfully")
        except Exception as e:
            raise RuntimeError(
                f"[openstack-exporter] Failed to deploy openstack-database-exporter: {e}"
            )

    # =================================================================
    # pre_install
    # =================================================================
    def pre_install(self, kubectl):
        log.debug("[openstack-exporter] Starting pre-install...")

        # 1) Deploy openstack-exporter (Deployment + Service)
        self._deploy_openstack_exporter(kubectl)

        # 2) Fetch DB secrets and create DSN secret
        self._create_dsn_secret(kubectl)

        # 3) Deploy openstack-database-exporter (Deployment)
        self._deploy_database_exporter(kubectl)

        log.debug("[openstack-exporter] pre-install complete")
