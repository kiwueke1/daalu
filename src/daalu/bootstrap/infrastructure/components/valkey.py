# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/valkey.py

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import yaml

from daalu.bootstrap.engine.component import InfraComponent


class ValkeyComponent(InfraComponent):
    """
    Deploy Valkey with TLS using cert-manager and Helm.
    Migrated from atmosphere Ansible role: roles/valkey
    """

    def __init__(
        self,
        *,
        values_path: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        chart_dir: Optional[Path] = None,
        registry_url: Optional[str] = None,
        registry_project: str = "openstack",
    ):
        super().__init__(
            name="valkey",
            repo_name="local",
            repo_url="",
            chart="valkey",
            version=None,
            namespace=namespace,
            release_name="valkey",
            local_chart_dir=chart_dir
            or values_path.parent / "charts",
            remote_chart_dir=Path("/usr/local/src/valkey"),
            kubeconfig=kubeconfig,
            uses_helm=True,
        )

        self.values_path = values_path
        self.wait_for_pods = True
        self.min_running_pods = 1
        self.enable_argocd = False
        self._registry_url = registry_url
        self._registry_project = registry_project

        self._values: Dict = yaml.safe_load(values_path.read_text()) or {}

    # ------------------------------------------------------------------
    def pre_install(self, kubectl) -> None:
        """
        Create TLS resources (CA, Issuer, Server Certificate).
        Exact parity with Ansible role.
        """

        ns = self.namespace

        kubectl.apply_objects(
            [
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Certificate",
                    "metadata": {"name": "valkey-ca", "namespace": ns},
                    "spec": {
                        "commonName": "valkey-ca",
                        "duration": "87600h",
                        "isCA": True,
                        "issuerRef": {
                            "group": "cert-manager.io",
                            "kind": "ClusterIssuer",
                            "name": "self-signed",
                        },
                        "privateKey": {"algorithm": "RSA", "size": 2048},
                        "renewBefore": "720h",
                        "secretName": "valkey-ca",
                    },
                },
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Issuer",
                    "metadata": {"name": "valkey", "namespace": ns},
                    "spec": {"ca": {"secretName": "valkey-ca"}},
                },
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Certificate",
                    "metadata": {"name": "valkey-server", "namespace": ns},
                    "spec": {
                        "commonName": "valkey",
                        "dnsNames": [
                            "127.0.0.1",
                            "localhost",
                            "valkey.openstack.svc.cluster.local",
                            "*.valkey.openstack.svc.cluster.local",
                            "valkey-headless.openstack.svc.cluster.local",
                            "*.valkey-headless.openstack.svc.cluster.local",
                        ],
                        "duration": "87600h",
                        "issuerRef": {
                            "group": "cert-manager.io",
                            "kind": "Issuer",
                            "name": "valkey",
                        },
                        "privateKey": {"algorithm": "RSA", "size": 2048},
                        "renewBefore": "720h",
                        "secretName": "valkey-server-certs",
                    },
                },
            ]
        )

    # ------------------------------------------------------------------
    def values(self) -> Dict:
        """
        Helm values for Valkey.
        """
        data = dict(self._values)
        if self._registry_url:
            from daalu.bootstrap.registry.image_mirror import ImageMirror

            # Bitnami charts use global.imageRegistry as the authoritative
            # registry override — it takes precedence over per-image .registry.
            data.setdefault("global", {})["imageRegistry"] = self._registry_url

            def _rewrite_repo(src_registry: str, src_repo: str, src_tag: str) -> str:
                src_image = f"{src_registry}/{src_repo}:{src_tag}"
                rewritten = ImageMirror.rewrite_image_static(
                    src_image, self._registry_url, self._registry_project
                )
                parts = rewritten.split("/", 1)
                return parts[1].rsplit(":", 1)[0] if len(parts) > 1 else rewritten.rsplit(":", 1)[0]

            # Rewrite main image
            img = data.get("image", {})
            if img.get("registry") and img.get("repository"):
                img["repository"] = _rewrite_repo(img["registry"], img["repository"], img.get("tag", "latest"))
                img.pop("registry", None)

            # Rewrite sentinel image
            sentinel_img = data.get("sentinel", {}).get("image", {})
            if sentinel_img.get("registry") and sentinel_img.get("repository"):
                sentinel_img["repository"] = _rewrite_repo(sentinel_img["registry"], sentinel_img["repository"], sentinel_img.get("tag", "latest"))
                sentinel_img.pop("registry", None)

            # Rewrite metrics/exporter image
            metrics_img = data.get("metrics", {}).get("image", {})
            if metrics_img.get("registry") and metrics_img.get("repository"):
                metrics_img["repository"] = _rewrite_repo(metrics_img["registry"], metrics_img["repository"], metrics_img.get("tag", "latest"))
                metrics_img.pop("registry", None)

        return data
