from pathlib import Path
from daalu.bootstrap.engine.component import InfraComponent


class MetalLBComponent(InfraComponent):
    def __init__(
        self,
        *,
        values_path: Path,
        metallb_config_path: Path,
        kubeconfig: str,
    ):
        super().__init__(
            name="metallb",
            repo_name="metallb",
            repo_url="https://metallb.github.io/metallb",
            chart="metallb",
            version=None,
            namespace="metallb-system",
            release_name="metallb",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src"),
            kubeconfig=kubeconfig,
        )

        self.values_path = values_path
        self.metallb_config_path = metallb_config_path
        self.wait_for_pods = True
        self.min_running_pods = 2

    # ------------------------------------------------------------------
    # Helm values (from assets)
    # ------------------------------------------------------------------

    def values(self) -> dict:
        return self.load_values_file(self.values_path)

    # ------------------------------------------------------------------
    # Post-install: apply address pool config
    # ------------------------------------------------------------------

    def post_install(self, kubectl) -> None:
        content = self.metallb_config_path.read_text()
        kubectl.apply_content(content)
