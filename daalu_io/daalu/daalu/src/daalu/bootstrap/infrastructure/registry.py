# src/daalu/bootstrap/infrastructure/registry.py


from __future__ import annotations
from pathlib import Path
from typing import List

from daalu.bootstrap.infrastructure.components.metallb import MetalLBComponent
from daalu.bootstrap.infrastructure.models import InfraSelection
from daalu.bootstrap.infrastructure.engine.component import InfraComponent


def infra_asset_path(
    workspace_root: Path,
    component: str,
    filename: str = "config.yaml",
) -> Path:
    return (
        workspace_root
        / "src/daalu/bootstrap"
        / "infrastructure"
        / "assets"
        / component
        / filename
    )


def build_infrastructure_components(
    *,
    selection: InfraSelection,
    workspace_root: Path,
    kubeconfig_path: str,
) -> list[InfraComponent]:
    components: list[InfraComponent] = []

    if selection.components is None or "metallb" in selection.components:
        components.append(
            MetalLBComponent(
                metallb_config_path=infra_asset_path(
                    workspace_root,
                    component="metallb",
                    filename="config.yaml",
                ),
                kubeconfig=kubeconfig_path,
            )
        )

    # ---- Future infra components ----
    # if selection.components is None or "argocd" in selection.components:
    #     components.append(
    #         ArgoCDComponent(
    #             config_path=infra_asset_path(workspace_root, "argocd"),
    #             kubeconfig=kubeconfig_path,
    #         )
    #     )

    return components
