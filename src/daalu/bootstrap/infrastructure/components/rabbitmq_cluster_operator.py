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
            # Override the operator's DEFAULT_RABBITMQ_IMAGE to point at Harbor.
            # The chart sets this via rabbitmqImage.registry + .repository + .tag.
            # We split on the last ':' to get tag, then rewrite the registry/repo.
            rmq_img = data.get("rabbitmqImage", {})
            registry = rmq_img.get("registry", "docker.io")
            repository = rmq_img.get("repository", "library/rabbitmq")
            tag = rmq_img.get("tag", "")
            src_image = f"{registry}/{repository}:{tag}" if tag else f"{registry}/{repository}"
            from daalu.bootstrap.registry.image_mirror import ImageMirror
            rewritten = ImageMirror.rewrite_image_static(
                src_image, self._registry_url, self._registry_project
            )
            # Split rewritten back into registry+repo and tag
            if ":" in rewritten.split("/")[-1]:
                repo_part, tag_part = rewritten.rsplit(":", 1)
            else:
                repo_part, tag_part = rewritten, ""
            # registry is the first component (host:port or host)
            parts = repo_part.split("/", 1)
            data.setdefault("rabbitmqImage", {})["registry"] = parts[0]
            data["rabbitmqImage"]["repository"] = parts[1] if len(parts) > 1 else ""
            if tag_part:
                data["rabbitmqImage"]["tag"] = tag_part
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

