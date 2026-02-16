# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/monitoring/components/minio.py

# src/daalu/bootstrap/monitoring/components/minio.py

from pathlib import Path

from daalu.bootstrap.engine.component import InfraComponent
from daalu.utils.helpers import load_yaml_file


class MinIOComponent(InfraComponent):
    def __init__(
        self,
        *,
        kubeconfig: str,
        values_path: Path,
        assets_dir: Path,
        namespace: str = "object-storage",
        enable_argocd: bool = False,
        **kwargs,
    ):
        super().__init__(
            name="minio",
            repo_name="local",
            repo_url="",
            chart="minio",
            version=None,
            namespace=namespace,
            release_name="minio",
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src"),
            enable_argocd=enable_argocd,
        )

        self.values_path = values_path

        # ALWAYS define _values
        self._values = (
            load_yaml_file(values_path)
            if values_path and values_path.exists()
            else {}
        )
