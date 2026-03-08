# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/istio/istiod.py

from pathlib import Path
from daalu.bootstrap.engine.component import InfraComponent


class IstiodComponent(InfraComponent):
    def __init__(
        self,
        *,
        assets_dir: Path,
        kubeconfig: str,
        registry_url: str | None = None,
        registry_project: str = "openstack",
    ):
        super().__init__(
            name="istiod",
            repo_name=None,
            repo_url=None,
            uses_helm=True,
            chart="istio-control/istio-discovery",
            version=None,
            namespace="istio-system",
            release_name="istiod",
            local_chart_dir=assets_dir,
            remote_chart_dir=Path("/usr/local/src/istio"),
            kubeconfig=kubeconfig,
        )

        self.assets_dir = assets_dir
        self.min_running_pods = 1
        self.enable_argocd = False
        self._registry_url = registry_url
        self._registry_project = registry_project

    def values_file(self) -> Path:
        return self.assets_dir / "istiod-values.yaml"

    def values(self) -> dict:
        data = self.load_values_file(self.values_file())
        if self._registry_url:
            data.setdefault("global", {})["hub"] = (
                f"{self._registry_url}/{self._registry_project}/istio"
            )
        return data
