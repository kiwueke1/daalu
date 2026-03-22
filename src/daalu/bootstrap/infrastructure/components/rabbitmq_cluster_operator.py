# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/rabbitmq_cluster_operator.py

from __future__ import annotations

from pathlib import Path
from typing import Optional

from daalu.bootstrap.engine.component import InfraComponent


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
        registry_url: str | None = None,
        registry_project: str = "openstack",
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
        self._registry_url = registry_url
        self._registry_project = registry_project

        self._values: dict = {}

    # ------------------------------------------------------------------
    # CRDs must exist before Helm
    # ------------------------------------------------------------------
    def pre_install(self, kubectl) -> None:
        crd_root = (
            self.assets_dir
            / "rabbitmq-cluster-operator"
            / "crds"
        )

        for subdir in [
            "messaging-topology-operator",
            "cluster-operator",
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

    def values(self) -> dict:
        data = self.load_values_file(self.values_path)
        if self._registry_url:
            from daalu.bootstrap.registry.image_mirror import ImageMirror

            # Bitnami charts use global.imageRegistry as the authoritative registry
            # override — it takes precedence over per-image .registry fields.
            # Set it to the Harbor host:port so all images in this chart resolve to Harbor.
            data.setdefault("global", {})["imageRegistry"] = self._registry_url

            # Rewrite each image's repository to include the Harbor project prefix.
            # global.imageRegistry + repository:tag → full image URL.
            def _rewrite_repo(src_registry: str, src_repo: str, src_tag: str) -> str:
                src_image = f"{src_registry}/{src_repo}:{src_tag}"
                rewritten = ImageMirror.rewrite_image_static(
                    src_image, self._registry_url, self._registry_project
                )
                # Strip the leading registry host (global.imageRegistry) — keep only repo:tag
                parts = rewritten.split("/", 1)
                return parts[1] if len(parts) > 1 else rewritten

            rmq = data.get("rabbitmqImage", {})
            data.setdefault("rabbitmqImage", {})["repository"] = _rewrite_repo(
                rmq.get("registry", "docker.io"),
                rmq.get("repository", "library/rabbitmq"),
                rmq.get("tag", ""),
            ).rsplit(":", 1)[0]  # strip tag — tag field is kept separately

            cred = data.get("credentialUpdaterImage", {})
            data.setdefault("credentialUpdaterImage", {})["repository"] = _rewrite_repo(
                "docker.io",
                cred.get("repository", "rabbitmqoperator/credential-updater"),
                cred.get("tag", ""),
            ).rsplit(":", 1)[0]

            cop = data.get("clusterOperator", {}).get("image", {})
            data.setdefault("clusterOperator", {}).setdefault("image", {})["repository"] = (
                _rewrite_repo(
                    "docker.io",
                    cop.get("repository", "rabbitmqoperator/cluster-operator"),
                    cop.get("tag", ""),
                ).rsplit(":", 1)[0]
            )

            mto = data.get("msgTopologyOperator", {}).get("image", {})
            data.setdefault("msgTopologyOperator", {}).setdefault("image", {})["repository"] = (
                _rewrite_repo(
                    "docker.io",
                    mto.get("repository", "rabbitmqoperator/messaging-topology-operator"),
                    mto.get("tag", ""),
                ).rsplit(":", 1)[0]
            )

        return data

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

