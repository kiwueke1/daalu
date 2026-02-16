# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/percona_xtradb_cluster.py

from __future__ import annotations

import secrets
import string
from pathlib import Path
from typing import Optional

import yaml

from daalu.bootstrap.engine.component import InfraComponent


def _gen_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class PerconaXtraDBClusterComponent(InfraComponent):
    """
    Deploy Percona XtraDB Cluster CR, secrets, and HAProxy metrics service.
    Mirrors percona_xtradb_cluster Ansible role.
    """

    def __init__(
        self,
        *,
        spec_path: Path,
        kubeconfig: str,
    ):
        super().__init__(
            name="percona-xtradb-cluster",
            repo_name="none",
            repo_url="",
            chart="",
            version=None,
            namespace="openstack",
            release_name="percona-xtradb",
            local_chart_dir=Path("/tmp"),
            remote_chart_dir=Path("/tmp"),
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self.spec_path = spec_path
        self.wait_for_pods = False

        self._values: Dict = {}

        raw = yaml.safe_load(spec_path.read_text())

        if not raw:
            raise ValueError(
                f"{spec_path} is empty. "
                "You must define _percona_xtradb_cluster_spec."
            )

        if "_percona_xtradb_cluster_spec" not in raw:
            raise ValueError(
                f"{spec_path} must contain a top-level "
                "'_percona_xtradb_cluster_spec' key."
            )

        self.cluster_spec = raw["_percona_xtradb_cluster_spec"]


    # ------------------------------------------------------------------
    def pre_install(self, kubectl) -> None:
        """
        Create secret if it doesn't exist (exact Ansible parity).
        """
        secret_name = self.cluster_spec.get("secretsName", "percona-xtradb")

        try:
            if kubectl.get(
                api_version="v1",
                kind="Secret",
                name=secret_name,
                namespace=self.namespace,
            ):
                return
        except RuntimeError:
            pass  # Secret doesn't exist yet — create it below

        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": secret_name,
                    "namespace": self.namespace,
                },
                "type": "Opaque",
                "stringData": {
                    "clustercheck": _gen_password(),
                    "monitor": _gen_password(),
                    "operator": _gen_password(),
                    "proxyadmin": _gen_password(),
                    "replication": _gen_password(),
                    "root": _gen_password(),
                    "xtrabackup": _gen_password(),
                },
            }
        ])

    # ------------------------------------------------------------------
    def post_install(self, kubectl) -> None:
        """
        Apply PerconaXtraDBCluster CR and HAProxy metrics service.
        """

        kubectl.apply_objects([
            {
                "apiVersion": "pxc.percona.com/v1",
                "kind": "PerconaXtraDBCluster",
                "metadata": {
                    "name": "percona-xtradb",
                    "namespace": self.namespace,
                },
                "spec": self.cluster_spec,
            }
        ])

        # HAProxy metrics service
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": "percona-xtradb-haproxy-metrics",
                    "namespace": self.namespace,
                    "labels": {
                        "name": "percona-xtradb-haproxy-metrics",
                    },
                },
                "spec": {
                    "type": "ClusterIP",
                    "ports": [
                        {
                            "name": "metrics",
                            "port": 8404,
                            "protocol": "TCP",
                            "targetPort": 8404,
                        }
                    ],
                    "selector": {
                        "app.kubernetes.io/component": "haproxy",
                        "app.kubernetes.io/instance": "percona-xtradb",
                    },
                },
            }
        ])
