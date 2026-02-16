# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/monitoring/components/opensearch.py

import os
from pathlib import Path
from typing import Dict

from daalu.bootstrap.engine.component import InfraComponent
from daalu.utils.helpers import load_yaml_file


class OpenSearchComponent(InfraComponent):
    """
    Daalu migration of the Atmosphere OpenSearch Ansible role.

    Responsibilities:
    - Deploy OpenSearch Helm chart
    - Optionally onboard into Argo CD
    """

    def __init__(
        self,
        *,
        kubeconfig: str,
        assets_dir: Path,
        values_path: Path,
        namespace: str = "observability",
        enable_argocd: bool = False,
        admin_password: str = "",
        **kwargs,
    ):
        super().__init__(
            name="opensearch",
            repo_name="local",
            repo_url="",
            chart="opensearch",
            version=None,
            namespace=namespace,
            release_name="opensearch",
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src"),
            enable_argocd=enable_argocd,
        )

        # Load values once
        self._values: Dict = load_yaml_file(values_path) if values_path else {}
        self.assets_dir = assets_dir
        self.admin_password = admin_password or os.environ.get("DAALU_OPENSEARCH_ADMIN_PASSWORD", "")

    def values(self) -> Dict:
        """
        Helm values (equivalent to:
        _opensearch_helm_values | combine(opensearch_helm_values)
        """
        return self._values or {}

    def pre_install(self, kubectl):

        # 1) Create secrets
        kubectl.apply_objects(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": "observability"},
                },
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": "opensearch-admin-password",
                        "namespace": "observability",
                    },
                    "type": "Opaque",
                    "stringData": {
                        "OPENSEARCH_INITIAL_ADMIN_PASSWORD": self.admin_password,
                    },
                },
            ]
        )




