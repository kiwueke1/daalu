# src/daalu/bootstrap/infrastructure/components/argocd.py

from pathlib import Path
from daalu.bootstrap.infrastructure.engine.component import InfraComponent

class ArgoCDComponent(InfraComponent):
    def __init__(self, *, values_path: Path, kubeconfig: str):
        super().__init__(
            name="argocd",
            repo_name="argo",
            repo_url="https://argoproj.github.io/argo-helm",
            chart="argo-cd",
            version=None,
            namespace="argocd",
            release_name="argocd",
            local_chart_dir=Path.home() / ".daalu/helm/charts",
            remote_chart_dir=Path("/usr/local/src"),
            kubeconfig=kubeconfig,
        )

        self.values_path = values_path
        self.wait_for_pods = True
        self.min_running_pods = 1

    def values(self) -> dict:
        return self.load_values_file(self.values_path)