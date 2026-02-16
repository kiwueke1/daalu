# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/istio/base.py

from pathlib import Path
from daalu.bootstrap.engine.component import InfraComponent


class IstioBaseComponent(InfraComponent):
    def __init__(self, *, assets_dir: Path, kubeconfig: str):
        super().__init__(
            name="istio-base",
            repo_name=None, 
            repo_url=None, 
            chart="base",
            version=None,
            namespace="istio-system",
            release_name="istio-base",
            local_chart_dir=assets_dir,
            remote_chart_dir=Path("/usr/local/src/istio"),
            kubeconfig=kubeconfig,
            uses_helm=True,          # explicit
        )
        self.assets_dir = assets_dir
        self.min_running_pods = 0  # CRDs only
        self.enable_argocd = False
        self._values: Dict = {}

    def values_file(self) -> Path:
        return self.assets_dir / "base-values.yaml"
