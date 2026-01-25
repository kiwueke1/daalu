# src/daalu/bootstrap/infrastructure/components/valkey.py

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import yaml

from daalu.bootstrap.infrastructure.engine.component import InfraComponent


class ValkeyComponent(InfraComponent):
    """
    Deploy Valkey with TLS using cert-manager and Helm.
    Migrated from atmosphere Ansible role: roles/valkey
    """

    def __init__(
        self,
        *,
        values_path: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        chart_dir: Optional[Path] = None,
    ):
        super().__init__(
            name="valkey",
            repo_name="local",
            repo_url="",
            chart="valkey",
            version=None,
            namespace=namespace,
            release_name="valkey",
            local_chart_dir=chart_dir
            or values_path.parent / "charts",
            remote_chart_dir=Path("/usr/local/src/valkey"),
            kubeconfig=kubeconfig,
            uses_helm=True,
        )

        self.values_path = values_path
        self.wait_for_pods = True
        self.min_running_pods = 1
        self.enable_argocd = False

        self._values: Dict = yaml.safe_load(values_path.read_text()) or {}

    # ------------------------------------------------------------------
    def pre_install(self, kubectl) -> None:
        """
        Create TLS resources (CA, Issuer, Server Certificate).
        Exact parity with Ansible role.
        """

        ns = self.namespace

        kubectl.apply_objects(
            [
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Certificate",
                    "metadata": {"name": "valkey-ca", "namespace": ns},
                    "spec": {
                        "commonName": "valkey-ca",
                        "duration": "87600h",
                        "isCA": True,
                        "issuerRef": {
                            "group": "cert-manager.io",
                            "kind": "ClusterIssuer",
                            "name": "self-signed",
                        },
                        "privateKey": {"algorithm": "RSA", "size": 2048},
                        "renewBefore": "720h",
                        "secretName": "valkey-ca",
                    },
                },
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Issuer",
                    "metadata": {"name": "valkey", "namespace": ns},
                    "spec": {"ca": {"secretName": "valkey-ca"}},
                },
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Certificate",
                    "metadata": {"name": "valkey-server", "namespace": ns},
                    "spec": {
                        "commonName": "valkey",
                        "dnsNames": [
                            "127.0.0.1",
                            "localhost",
                            "valkey.openstack.svc.cluster.local",
                            "*.valkey.openstack.svc.cluster.local",
                            "valkey-headless.openstack.svc.cluster.local",
                            "*.valkey-headless.openstack.svc.cluster.local",
                        ],
                        "duration": "87600h",
                        "issuerRef": {
                            "group": "cert-manager.io",
                            "kind": "Issuer",
                            "name": "valkey",
                        },
                        "privateKey": {"algorithm": "RSA", "size": 2048},
                        "renewBefore": "720h",
                        "secretName": "valkey-server-certs",
                    },
                },
            ]
        )

    # ------------------------------------------------------------------
    def values(self) -> Dict:
        """
        Helm values for Valkey.
        """
        return self._values
