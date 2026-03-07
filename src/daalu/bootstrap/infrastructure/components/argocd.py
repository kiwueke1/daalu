# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/argocd.py

from pathlib import Path
from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.registry.image_mirror import ImageMirror


def _deep_merge(base: dict, overrides: dict) -> dict:
    result = dict(base)
    for k, v in overrides.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class ArgoCDComponent(InfraComponent):
    def __init__(
        self,
        *,
        values_path: Path,
        kubeconfig: str,
        harbor_url: str | None = None,
        harbor_project: str = "openstack",
    ):
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
        self.enable_argocd = False
        self._harbor_url = harbor_url
        self._harbor_project = harbor_project

    def values(self) -> dict:
        data = self.load_values_file(self.values_path)
        if not self._harbor_url:
            return data

        def _rw(repo: str) -> str:
            return ImageMirror.rewrite_image_static(repo, self._harbor_url, self._harbor_project)

        harbor_overrides = {
            "global": {
                "image": {"repository": _rw("quay.io/argoproj/argocd")},
            },
            "redis": {
                "image": {"repository": _rw("redis")},
            },
            "dex": {
                "image": {"repository": _rw("ghcr.io/dex-idp/dex")},
            },
            "redisSecretInit": {
                "image": {"repository": _rw("quay.io/argoproj/argocd")},
            },
        }

        return _deep_merge(data, harbor_overrides)