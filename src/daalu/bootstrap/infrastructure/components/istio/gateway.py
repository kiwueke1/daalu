# src/daalu/bootstrap/infrastructure/components/istio/gateway.py

from pathlib import Path
from daalu.bootstrap.engine.component import InfraComponent


class IstioGatewayComponent(InfraComponent):
    def __init__(
        self,
        *,
        name: str,
        namespace: str,
        assets_dir: Path,
        kubeconfig: str,
    ):
        super().__init__(
            name=name,
            repo_name=None,
            repo_url=None,
            uses_helm=True, 
            chart="gateway",
            version=None,
            namespace=namespace,
            release_name=name,
            local_chart_dir=assets_dir,
            remote_chart_dir=Path("/usr/local/src/istio"),
            kubeconfig=kubeconfig,
        )

        self.assets_dir = assets_dir
        self.min_running_pods = 1
        self.enable_argocd = False

        self._values: Dict = {}

    def values_file(self) -> Path:
        return self.assets_dir / f"{self.release_name}-values.yaml"
