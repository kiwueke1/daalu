# src/daalu/bootstrap/infrastructure/components/rabbitmq_cluster_operator.py

from __future__ import annotations

from pathlib import Path
from typing import Optional

from daalu.bootstrap.infrastructure.engine.component import InfraComponent


class RabbitMQClusterOperatorComponent(InfraComponent):
    """
    Deploy RabbitMQ Cluster Operator.
    Handles CRDs explicitly before Helm install.
    Mirrors atmosphere rabbitmq_cluster_operator Ansible role.
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
            name="rabbitmq-cluster-operator",
            repo_name="rabbitmq-cluster-operator",
            repo_url="",
            chart="rabbitmq-cluster-operator",
            version=None,
            namespace="openstack",
            release_name="rabbitmq-cluster-operator",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src"),
            kubeconfig=kubeconfig,
        )

        self.values_path = values_path
        self.assets_dir = assets_dir
        self.github_token = github_token
        self.wait_for_pods = True

    # ------------------------------------------------------------------
    # CRDs must exist before Helm
    # ------------------------------------------------------------------
    def pre_install(self, kubectl) -> None:
        crd_root = (
            self.assets_dir
            / "charts"
            / "rabbitmq-cluster-operator"
            / "crds"
        )

        for subdir in [
            "messaging-topology-operator",
            "rabbitmq-cluster",
        ]:
            path = crd_root / subdir
            if not path.exists():
                continue

            for yaml_file in sorted(path.glob("*.yaml")):
                kubectl.apply_file(
                    yaml_file,
                    server_side=True,
                    field_manager="atmosphere",
                    force_conflicts=True,
                )

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
            app_name="rabbitmq-cluster-operator",
            github_repo="kiwueke1/argocd-infrastructure-app",
            github_path=(
                "apps/openstack/"
                "rabbitmq-cluster-operator/"
                "rabbitmq-cluster-operator.yaml"
            ),
            github_token=self.github_token,
        )

