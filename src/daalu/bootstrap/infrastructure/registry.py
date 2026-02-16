# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/registry.py


from __future__ import annotations
from pathlib import Path
from typing import List

from daalu.bootstrap.infrastructure.models import InfraSelection
from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.infrastructure.components.metallb import MetalLBComponent
from daalu.bootstrap.infrastructure.components.argocd import ArgoCDComponent
from daalu.bootstrap.infrastructure.components.jenkins import JenkinsComponent
from daalu.bootstrap.infrastructure.components.cert_manager import CertManagerComponent
from daalu.bootstrap.infrastructure.components.cluster_issuer import (
    ClusterIssuerComponent,
)
from daalu.bootstrap.infrastructure.components.istio.factory import build_istio_components
from daalu.bootstrap.infrastructure.utils.assets import infra_asset_path
from daalu.bootstrap.infrastructure.components.ingress_nginx import (
    IngressNginxComponent,
)
from daalu.bootstrap.infrastructure.components.rabbitmq_cluster_operator import (
    RabbitMQClusterOperatorComponent,
)
from daalu.bootstrap.infrastructure.components.percona_xtradb_cluster_operator import (
    PerconaXtraDBClusterOperatorComponent,
)
from daalu.bootstrap.infrastructure.components.percona_xtradb_cluster import (
    PerconaXtraDBClusterComponent,
)
from daalu.bootstrap.infrastructure.components.kubernetes_node_labels import (
    KubernetesNodeLabelsComponent,
)
from daalu.bootstrap.infrastructure.components.valkey import ValkeyComponent
from daalu.bootstrap.infrastructure.components.keycloak import KeycloakComponent
from daalu.bootstrap.infrastructure.components.keepalived import (
    KeepalivedComponent,
)



def build_infrastructure_components(
    *,
    selection: InfraSelection,
    workspace_root: Path,
    kubeconfig_path: str,
    keycloak_admin_password: str = "",
) -> list[InfraComponent]:
    components: list[InfraComponent] = []

    if selection.components is None or "metallb" in selection.components:
        components.append(
            MetalLBComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="metallb",
                    filename="values.yaml",
                ),
                metallb_config_path=infra_asset_path(
                    workspace_root,
                    component="metallb",
                    filename="config.yaml",
                ),
                kubeconfig=kubeconfig_path,
            )
        )
    if selection.components is None or "argocd" in selection.components:
        components.append(
            ArgoCDComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="argocd",
                    filename="values.yaml",
                ),
                kubeconfig=kubeconfig_path
            )
        )


    if selection.components is None or "cert-manager" in selection.components:
        components.append(
            CertManagerComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="cert-manager",
                    filename="values.yaml",
                ),
                config_path=infra_asset_path(
                    workspace_root,
                    component="cert-manager",
                    filename="config.yaml",
                ),
                assets_dir=infra_asset_path(
                    workspace_root,
                    component="cert-manager",
                    filename="",
                ),
                kubeconfig=kubeconfig_path,
            )
        )
        
    if selection.components is None or "cluster-issuer" in selection.components:
        components.append(
            ClusterIssuerComponent(
                config_path=infra_asset_path(
                    workspace_root,
                    component="cluster_issuer",
                    filename="config.yaml",
                ),
                kubeconfig=kubeconfig_path,
            )
        )

    if selection.components is None or "istio" in selection.components:
        components.extend(
            build_istio_components(
                workspace_root=workspace_root,
                kubeconfig_path=kubeconfig_path,
            )
        )

    if selection.components is None or "ingress-nginx" in selection.components:
        components.append(
            IngressNginxComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="ingress-nginx",
                    filename="values.yaml",
                ),
                assets_dir=infra_asset_path(
                    workspace_root,
                    component="ingress-nginx",
                    filename="",
                ),
                kubeconfig=kubeconfig_path,
                #github_token=selection.github_token,
            )
        )

    if (
        selection.components is None
        or "rabbitmq-cluster-operator" in selection.components
    ):
        components.append(
            RabbitMQClusterOperatorComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="rabbitmq-cluster-operator",
                    filename="values.yaml",
                ),
                assets_dir=infra_asset_path(
                    workspace_root,
                    component="rabbitmq-cluster-operator",
                    filename="",
                ),
                kubeconfig=kubeconfig_path,
                #github_token=selection.github_token,
            )
        )

    if selection.components is None or "pxc-operator" in selection.components:
        components.append(
            PerconaXtraDBClusterOperatorComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="pxc-operator",
                    filename="values.yaml",
                ),
                assets_dir=infra_asset_path(
                    workspace_root,
                    component="pxc-operator",
                    filename="",
                ),
                kubeconfig=kubeconfig_path,
                #github_token=selection.github_token,
            )
        )

    if selection.components is None or "percona-xtradb-cluster" in selection.components:
        components.append(
            PerconaXtraDBClusterComponent(
                spec_path=infra_asset_path(
                    workspace_root,
                    component="percona-xtradb-cluster",
                    filename="spec.yaml",
                ),
                kubeconfig=kubeconfig_path,
            )
        )

    if selection.components is None or "kubernetes-node-labels" in selection.components:
        components.append(
            KubernetesNodeLabelsComponent(
                workspace_root=workspace_root,
                kubeconfig=kubeconfig_path,
            )
        )

    if selection.components is None or "valkey" in selection.components:
        components.append(
            ValkeyComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="valkey",
                    filename="values.yaml",
                ),
                kubeconfig=kubeconfig_path,
            )
        )

    if selection.components is None or "keycloak" in selection.components:
        components.append(
            KeycloakComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="keycloak",
                    filename="values.yaml",
                ),
                kubeconfig=kubeconfig_path,
                admin_password=keycloak_admin_password,
            )
        )

    if selection.components is None or "keepalived" in selection.components:
        components.append(
            KeepalivedComponent(
                assets_dir=infra_asset_path(
                    workspace_root,
                    component="keepalived",
                    filename="",
                ),
                kubeconfig=kubeconfig_path,
            )
        )


    return components
