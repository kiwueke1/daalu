# src/daalu/bootstrap/monitoring/components/vector.py

from pathlib import Path
from daalu.bootstrap.engine.component import InfraComponent


class VectorComponent(InfraComponent):
    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "monitoring",
    ):
        super().__init__(
            name="vector",
            repo_name="local",
            repo_url="",
            chart="vector",
            version=None,
            namespace=namespace,
            release_name="vector",
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
