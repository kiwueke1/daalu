# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/istio/argocd.py

from pathlib import Path
import urllib.request

from daalu.bootstrap.engine.component import InfraComponent


class IstioArgoCDComponent(InfraComponent):
    def __init__(
        self,
        *,
        kubeconfig: str,
        github_token: str,
        repo_owner: str,
        repo_name: str,
        apps: dict[str, str],
    ):
        super().__init__(
            name="istio-argocd",
            repo_name="none",
            repo_url="",
            chart="",
            version=None,
            namespace="argocd",
            release_name="istio-argocd",
            local_chart_dir=Path("/tmp"),
            remote_chart_dir=Path("/tmp"),
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self.github_token = github_token
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.apps = apps
        self.wait_for_pods = False
        self.enable_argocd = True

    # --------------------------------------------------
    def post_install(self, kubectl) -> None:
        existing = kubectl.get_names(
            kind="Application",
            api_version="argoproj.io/v1alpha1",
            namespace="argocd",
        )

        for name, path in self.apps.items():
            if name.lower() in [e.lower() for e in existing]:
                continue

            manifest = self._download_manifest(path)
            kubectl.apply_yaml(manifest)

    def _download_manifest(self, path: str) -> str:
        url = (
            f"https://api.github.com/repos/"
            f"{self.repo_owner}/{self.repo_name}/contents/{path}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github.v3.raw",
                "Authorization": f"token {self.github_token}",
                "User-Agent": "daalu-cli",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
