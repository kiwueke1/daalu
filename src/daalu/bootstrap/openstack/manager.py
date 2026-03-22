# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/manager.py

import logging

from daalu.bootstrap.engine.helm_engine import HelmInfraEngine
from daalu.bootstrap.engine.infra_logging import InfraJsonlLogger
from daalu.helm.cli_runner import HelmCliRunner
from daalu.kube.kubectl import KubectlRunner
from daalu.utils.ssh_runner import SSHRunner

log = logging.getLogger("daalu")


class OpenStackManager:
    def __init__(
        self,
        *,
        helm: HelmCliRunner,
        ssh: SSHRunner,
        registry_url: str | None = None,
        registry_project: str = "openstack",
    ):
        self.helm = helm
        self.ssh = ssh
        self.registry_url = registry_url
        self.registry_project = registry_project

    def _ensure_mariadb_alias(self, kubectl, namespace: str = "openstack") -> None:
        """
        Ensure a 'mariadb' Service + Endpoints exist in the OpenStack namespace.

        OSH charts hard-code DEPENDENCY_SERVICE=openstack:mariadb in their
        kubernetes-entrypoint init container.  We use Percona XtraDB instead,
        so we create a headless Service named 'mariadb' with a manual Endpoints
        object pointing at the percona-xtradb-haproxy ClusterIP.  This is
        idempotent: if both objects already exist and are correct, nothing changes.
        """
        haproxy_svc = kubectl.get_object(
            api_version="v1",
            kind="Service",
            name="percona-xtradb-haproxy",
            namespace=namespace,
        )
        if not haproxy_svc:
            log.warning(
                "[openstack] percona-xtradb-haproxy service not found in %s — "
                "skipping mariadb alias creation",
                namespace,
            )
            return

        cluster_ip = haproxy_svc.get("spec", {}).get("clusterIP", "")
        if not cluster_ip or cluster_ip == "None":
            log.warning(
                "[openstack] percona-xtradb-haproxy has no clusterIP — "
                "skipping mariadb alias creation"
            )
            return

        kubectl.apply_objects([
            # ClusterIP service with no selector (endpoints managed manually)
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "mariadb", "namespace": namespace},
                "spec": {
                    "ports": [{"name": "mysql", "port": 3306, "protocol": "TCP", "targetPort": 3306}],
                },
            },
            # Manual Endpoints pointing at percona-xtradb-haproxy ClusterIP
            {
                "apiVersion": "v1",
                "kind": "Endpoints",
                "metadata": {"name": "mariadb", "namespace": namespace},
                "subsets": [
                    {
                        "addresses": [{"ip": cluster_ip}],
                        "ports": [{"name": "mysql", "port": 3306, "protocol": "TCP"}],
                    }
                ],
            },
        ])
        log.info(
            "[openstack] mariadb alias → %s (%s) ensured in namespace %s",
            "percona-xtradb-haproxy",
            cluster_ip,
            namespace,
        )

    def deploy(self, components, *, phase: str | None = None):
        logger = InfraJsonlLogger()
        engine = HelmInfraEngine(
            helm=self.helm,
            ssh=self.ssh,
            logger=logger,
            registry_url=self.registry_url,
            registry_project=self.registry_project,
        )

        # Ensure the mariadb alias exists before any component runs.
        # OSH charts hard-code DEPENDENCY_SERVICE=openstack:mariadb; without
        # this alias their kubernetes-entrypoint init containers stall forever.
        if components:
            kubectl = KubectlRunner(
                ssh=self.ssh,
                kubeconfig=components[0].kubeconfig,
                logger=logger,
            )
            namespace = getattr(components[0], "namespace", "openstack")
            self._ensure_mariadb_alias(kubectl, namespace)

        for component in components:
            engine.deploy(component, phase=phase)
