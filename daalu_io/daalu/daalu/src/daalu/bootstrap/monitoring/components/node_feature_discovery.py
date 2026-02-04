# src/daalu/bootstrap/monitoring/components/node_feature_discovery.py

from pathlib import Path
from daalu.bootstrap.engine.component import InfraComponent


class NodeFeatureDiscoveryComponent(InfraComponent):
    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
    ):
        super().__init__(
            name="node-feature-discovery",
            repo_name="kubernetes-sigs",
            repo_url="",
            chart="node-feature-discovery",
            version=None,
            namespace="monitoring",
            release_name="node-feature-discovery",
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

    def pre_install(self, kubectl):
        """
        Install CRDs (mirrors Ansible role).
        """
        crds_dir = self.assets_dir() / "crds"
        if not crds_dir.exists():
            return

        for crd in sorted(crds_dir.glob("*.yaml")):
            kubectl.apply_file(
                crd,
                server_side=True,
                field_manager="daalu",
                force_conflicts=True,
            )
