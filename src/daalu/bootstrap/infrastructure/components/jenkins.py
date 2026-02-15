# src/daalu/bootstrap/infrastructure/components/jenkins.py

from pathlib import Path
from daalu.bootstrap.engine.component import InfraComponent


class JenkinsComponent(InfraComponent):
    def __init__(self, *, assets_dir: Path, kubeconfig: str):
        super().__init__(
            name="jenkins",
            repo_name="jenkins",
            repo_url="https://charts.jenkins.io",
            chart="jenkins",
            version=None,
            namespace="devops-tools",
            release_name="jenkins",
            local_chart_dir=Path.home() / ".daalu/helm/charts",
            remote_chart_dir=Path("/usr/local/src"),
            kubeconfig=kubeconfig,
        )

        self._assets_dir = assets_dir
        self.min_running_pods = 1

    # ------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------
    def assets_dir(self) -> Path:
        return self._assets_dir

    # ------------------------------------------------------------
    # Helm values
    # ------------------------------------------------------------
    def values_file(self) -> Path:
        return self.assets_dir() / "values.yaml"

    def values(self) -> dict:
        return self.load_values_file(self.values_file())

    # ------------------------------------------------------------
    # Pre-install
    # ------------------------------------------------------------
    def pre_install(self, kubectl) -> None:
        for name in ("storageclass.yaml", "rbac.yaml"):
            path = self.assets_dir() / name
            if path.exists():
                kubectl.apply_file(path)

    # ------------------------------------------------------------
    # Post-install
    # ------------------------------------------------------------
    def post_install(self, kubectl) -> None:
        super().post_install(kubectl)
