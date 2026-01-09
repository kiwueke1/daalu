# src/daalu/bootstrap/infrastructure/components/metallb.py

from pathlib import Path
from daalu.bootstrap.infrastructure.engine.component import InfraComponent


class MetalLBComponent(InfraComponent):
    def __init__(self, metallb_config_path: Path, kubeconfig: str):
        super().__init__(
            name="metallb",
            repo_name="metallb",
            repo_url="https://metallb.github.io/metallb",
            chart="metallb",
            version=None,
            namespace="metallb-system",
            release_name="metallb",
            local_chart_dir=Path.home() / ".daalu/helm/charts",
            remote_chart_dir=Path("/usr/local/src"),
            kubeconfig=kubeconfig,
        )
        self.metallb_config_path = metallb_config_path
        self.min_running_pods = 2


    def values(self) -> dict:
        return {
            "controller": {
                "logLevel": "info",
            },
            "speaker": {
                "tolerations": [
                    {
                        "key": "node-role.kubernetes.io/control-plane",
                        "operator": "Exists",
                        "effect": "NoSchedule",
                    }
                ]
            },
        }

    def post_install_1(self, kubectl) -> None:
        kubectl.apply_file(str(self.metallb_config_path))

    def post_install(self, kubectl) -> None:
        content = self.metallb_config_path.read_text()
        kubectl.apply_content(content)