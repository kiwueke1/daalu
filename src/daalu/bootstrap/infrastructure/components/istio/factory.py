# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/istio/factory.py
from __future__ import annotations

from pathlib import Path
from typing import List
import yaml

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.infrastructure.utils.assets import infra_asset_path

from .base import IstioBaseComponent
from .istiod import IstiodComponent
from .gateway import IstioGatewayComponent
from .traffic import IstioTrafficComponent


def build_istio_components(
    *,
    workspace_root: Path,
    kubeconfig_path: str,
) -> List[InfraComponent]:
    """
    Build all Istio-related infrastructure components.

    This encapsulates *only* Istio lifecycle concerns:
    - Helm installs (base, istiod, gateways)
    - Traffic objects (Gateway / VS / DR)

    Argo CD onboarding is intentionally excluded.
    """
    components: List[InfraComponent] = []

    istio_assets_dir = infra_asset_path(
        workspace_root,
        component="istio",
        filename="",
    )

    # --------------------------------------------------
    # 1) Helm installs: base + istiod
    # --------------------------------------------------
    components.append(
        IstioBaseComponent(
            assets_dir=istio_assets_dir,
            kubeconfig=kubeconfig_path,
        )
    )

    components.append(
        IstiodComponent(
            assets_dir=istio_assets_dir,
            kubeconfig=kubeconfig_path,
        )
    )

    # --------------------------------------------------
    # 2) Gateways
    # --------------------------------------------------
    components.append(
        IstioGatewayComponent(
            name="istio-ingressgateway",
            namespace="istio-ingress",
            assets_dir=istio_assets_dir,
            kubeconfig=kubeconfig_path,
        )
    )

    # --------------------------------------------------
    # 3) Traffic objects (Gateway / VS / DR)
    # --------------------------------------------------
    traffic_cfg = infra_asset_path(
        workspace_root,
        component="istio",
        filename="traffic.yaml",
    )
    if traffic_cfg.exists():
        components.append(
            IstioTrafficComponent(
                config_path=traffic_cfg,
                kubeconfig=kubeconfig_path,
            )
        )

    return components
