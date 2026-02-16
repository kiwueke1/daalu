# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/frr_k8s/frr_k8s.py

from __future__ import annotations

from pathlib import Path

from daalu.bootstrap.engine.component import InfraComponent
import logging

log = logging.getLogger("daalu")


class FrrK8sComponent(InfraComponent):
    """
    Daalu FRR-K8s component (Free Range Routing for Kubernetes).

    Deploys the frr-k8s Helm chart which provides BGP routing
    capabilities via the FRR routing suite. Used by the OVN BGP
    agent to advertise tenant networks via BGP.

    Deployed into the frr-k8s-system namespace.
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "frr-k8s-system",
        release_name: str = "frr-k8s",
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="frr-k8s",
            repo_name="local",
            repo_url="",
            chart="frr-k8s",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/frr-k8s"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self.wait_for_pods = True
        self.min_running_pods = 1

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------
    def assets_dir(self) -> Path:
        return self._assets_dir

    def values(self) -> dict:
        return self.load_values_file(self.values_path)

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        log.debug("[frr-k8s] Starting pre-install...")
        log.debug("[frr-k8s] pre-install complete")
