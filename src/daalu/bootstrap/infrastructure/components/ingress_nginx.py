# src/daalu/bootstrap/infrastructure/components/ingress_nginx.py

from __future__ import annotations

from pathlib import Path
from typing import Optional

from daalu.bootstrap.engine.component import InfraComponent
#from daalu.bootstrap.infrastructure.utils.github import download_raw_github_file


class IngressNginxComponent(InfraComponent):
    """
    Deploy ingress-nginx via Helm, then optionally onboard it to Argo CD.
    Mirrors atmosphere ingress_nginx Ansible role.
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        github_token: Optional[str] = None,
    ):
        super().__init__(
            name="ingress-nginx",
            repo_name="ingress-nginx",
            repo_url="",  # local chart
            chart="ingress-nginx",
            version=None,
            namespace="ingress-nginx",
            release_name="ingress-nginx",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src"),
            kubeconfig=kubeconfig,
        )

        self.values_path = values_path
        self.assets_dir = assets_dir
        self.github_token = github_token
        self.wait_for_pods = True

        self._values: Dict = {}

    # ------------------------------------------------------------------
    # Helm chart upload (Ansible dependency replacement)
    # ------------------------------------------------------------------
    #def pre_install(self, ssh) -> None:
    #    ssh.put_dir(
    #        local_path=self.local_chart_dir / "ingress-nginx",
    #        remote_path=self.remote_chart_dir / "ingress-nginx",
    #        release_name=self.release_name,
    #    )

    # ------------------------------------------------------------------
    def values_file(self) -> Path:
        return self.values_path

    # ------------------------------------------------------------------
    # Argo CD onboarding (post Helm bootstrap)
    # ------------------------------------------------------------------
    def post_install(self, kubectl) -> None:
        if not self.github_token:
            return

        self.ensure_argocd_app(
            kubectl=kubectl,
            app_name="ingress-nginx",
            github_repo="kiwueke1/argocd-infrastructure-app",
            github_path="apps/ingress-nginx/ingress-nginx.yaml",
            github_token=self.github_token,
        )

