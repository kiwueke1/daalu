# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/monitoring/components/prometheus_pushgateway.py

from pathlib import Path
from daalu.bootstrap.engine.component import InfraComponent


class PrometheusPushgatewayComponent(InfraComponent):
    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "monitoring",
    ):
        super().__init__(
            name="prometheus-pushgateway",
            repo_name="local",
            repo_url="",
            chart="prometheus-pushgateway",
            version=None,
            namespace=namespace,
            release_name="prometheus-pushgateway",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/prometheus-pushgateway"),
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
