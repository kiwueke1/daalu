# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/monitoring/components/goldpinger.py

from pathlib import Path
from daalu.bootstrap.engine.component import InfraComponent


class GoldpingerComponent(InfraComponent):
    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "monitoring",
    ):
        super().__init__(
            name="goldpinger",
            repo_name="local",
            repo_url="",
            chart="goldpinger",
            version=None,
            namespace=namespace,
            release_name="goldpinger",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src"),
            kubeconfig=kubeconfig,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir

        self.wait_for_pods = True
        self.min_running_pods = 1

    def assets_dir(self) -> Path:
        return self._assets_dir

    def values(self) -> dict:
        return self.load_values_file(self.values_path)
