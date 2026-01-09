from __future__ import annotations
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class InfraComponent(ABC):
    """
    Declarative definition of an infrastructure component.
    """

    # Identity
    name: str

    # Helm repository
    repo_name: str
    repo_url: str

    # Helm chart
    chart: str
    version: Optional[str]

    # Helm release
    namespace: str
    release_name: str

    # Chart handling
    local_chart_dir: Path
    remote_chart_dir: Path

    # Kubernetes
    kubeconfig: str

    # Optional hooks
    wait_for_pods: bool = True
    min_running_pods: int = 1

    # ------------------------
    # Hooks
    # ------------------------

    def values(self) -> dict:
        """Component-specific Helm values"""
        return {}

    def post_install(self, kubectl) -> None:
        """Optional post-install hook"""
        pass
