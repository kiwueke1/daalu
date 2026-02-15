# src/daalu/bootstrap/infrastructure/components/percona_xtradb_cluster_operator.py

from __future__ import annotations
from pathlib import Path
from typing import Optional

from daalu.bootstrap.engine.component import InfraComponent


class PerconaXtraDBClusterOperatorComponent(InfraComponent):
    """
    Deploy Percona XtraDB Cluster Operator via Helm
    + onboard to Argo CD.
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
            name="pxc-operator",
            repo_name="pxc-operator",
            repo_url="",
            chart="pxc-operator",
            version=None,
            namespace="openstack",
            release_name="pxc-operator",
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
    # Upload Helm chart (replaces vexxhost.kubernetes.upload_helm_chart)
    # ------------------------------------------------------------------
    #def pre_install(self, ssh) -> None:
    #    ssh.put_dir(
    #        local_path=self.local_chart_dir / "pxc-operator",
    #        remote_path=self.remote_chart_dir / "pxc-operator",
    #        release_name=self.release_name,
    #    )

    # ------------------------------------------------------------------
    def values_file(self) -> Path:
        return self.values_path

    # ------------------------------------------------------------------
    # Argo CD onboarding
    # ------------------------------------------------------------------
    def post_install(self, kubectl) -> None:
        if not self.github_token:
            return

        self.ensure_argocd_app(
            kubectl=kubectl,
            app_name="pxc-operator",
            github_repo="kiwueke1/argocd-infrastructure-app",
            github_path="apps/openstack/pxc-operator/pxc-operator.yaml",
            github_token=self.github_token,
        )
