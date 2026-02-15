# src/daalu/bootstrap/monitoring/registry.py

from pathlib import Path
from typing import List
from daalu.bootstrap.monitoring.models import MonitoringSelection
# -------------------------
# Components
# -------------------------
from daalu.bootstrap.monitoring.components.node_feature_discovery import (
    NodeFeatureDiscoveryComponent,
)
from daalu.bootstrap.monitoring.components.kube_prometheus_stack.kube_prometheus_stack import (
    KubePrometheusStackComponent,
)
from daalu.bootstrap.monitoring.components.loki import (
    LokiComponent,
)
from daalu.bootstrap.monitoring.components.vector import (
    VectorComponent,
)
from daalu.bootstrap.monitoring.components.goldpinger import (
    GoldpingerComponent,
)
from daalu.bootstrap.monitoring.components.ipmi_exporter import (
    IPMIExporterComponent,
)
from daalu.bootstrap.monitoring.components.prometheus_pushgateway import (
    PrometheusPushgatewayComponent,
)
from daalu.bootstrap.monitoring.components.thanos import ThanosComponent
from daalu.bootstrap.monitoring.components.opensearch import OpenSearchComponent

# -------------------------
# Shared utilities
# -------------------------
from daalu.bootstrap.infrastructure.utils.assets import infra_asset_path
from daalu.bootstrap.shared.keycloak.models import (
    KeycloakIAMConfig,
    KeycloakAdminAuth,
    KeycloakRealmSpec,
    KeycloakClientSpec,
)
from daalu.bootstrap.monitoring.components.minio import MinIOComponent

def build_monitoring_components(
    *,
    selection: MonitoringSelection,
    workspace_root: Path,
    kubeconfig_path: str,
    cfg,
):
    components: List = []

    # ------------------------------------------------------------
    # Resolve Keycloak IAM config ONCE (shared by all components)
    # ------------------------------------------------------------
    keycloak_cfg = None
    monitoring_cfg = getattr(cfg, "monitoring", None)

    if getattr(cfg, "keycloak", None) and getattr(cfg.keycloak, "monitoring", None):
        kc = cfg.keycloak.monitoring

        keycloak_cfg = KeycloakIAMConfig(
            k8s_namespace="monitoring",
            oidc_issuer_url=f"{kc.base_url}/realms/{kc.realm}",
            admin=KeycloakAdminAuth(
                base_url=kc.base_url,
                admin_realm=kc.admin_realm,
                admin_client_id=kc.admin_client_id,
                username=kc.username,
                password=kc.password,
                verify_tls=kc.verify_tls,
            ),
            realm=KeycloakRealmSpec(
                realm=kc.realm,
                display_name=kc.display_name,
                enabled=True,
            ),
            clients=[
                KeycloakClientSpec(
                    id="grafana",
                    roles=["admin", "editor", "viewer"],
                    oauth2_proxy=True,
                    redirect_uris=kc.grafana_redirect_uris,
                    port=3000,
                )
            ],
        )

    # ------------------------------------------------------------
    # Node Feature Discovery
    # ------------------------------------------------------------
    if selection.components is None or "node-feature-discovery" in selection.components:
        components.append(
            NodeFeatureDiscoveryComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="node-feature-discovery",
                    filename="values.yaml",
                ),
                assets_dir=infra_asset_path(
                    workspace_root,
                    component="node-feature-discovery",
                ),
                kubeconfig=kubeconfig_path,
            )
        )

    # ------------------------------------------------------------
    # Kube Prometheus Stack (Grafana + Alertmanager + IAM)
    # ------------------------------------------------------------
    if selection.components is None or "kube-prometheus-stack" in selection.components:
        components.append(
            KubePrometheusStackComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="kube-prometheus-stack",
                    filename="values.yaml",
                ),
                assets_dir=infra_asset_path(
                    workspace_root,
                    component="kube-prometheus-stack",
                ),
                kubeconfig=kubeconfig_path,
                keycloak_config=keycloak_cfg,
            )
        )

    # ------------------------------------------------------------
    # Loki
    # ------------------------------------------------------------
    if selection.components is None or "loki" in selection.components:
        components.append(
            LokiComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="loki",
                    filename="values.yaml",
                ),
                assets_dir=infra_asset_path(
                    workspace_root,
                    component="loki",
                ),
                kubeconfig=kubeconfig_path,
                enable_argocd=False,
            )
        )

    # ------------------------------------------------------------
    # Vector (log agent → Loki)
    # ------------------------------------------------------------
    if selection.components is None or "vector" in selection.components:
        components.append(
            VectorComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="vector",
                    filename="values.yaml",
                ),
                assets_dir=infra_asset_path(
                    workspace_root,
                    component="vector",
                ),
                kubeconfig=kubeconfig_path,
            )
        )

    # ------------------------------------------------------------
    # Goldpinger
    # ------------------------------------------------------------
    if selection.components is None or "goldpinger" in selection.components:
        components.append(
            GoldpingerComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="goldpinger",
                    filename="values.yaml",
                ),
                assets_dir=infra_asset_path(
                    workspace_root,
                    component="goldpinger",
                ),
                kubeconfig=kubeconfig_path,
            )
        )

    # ------------------------------------------------------------
    # IPMI Exporter (raw DaemonSet)
    # ------------------------------------------------------------
    if selection.components is None or "ipmi-exporter" in selection.components:
        components.append(
            IPMIExporterComponent(
                kubeconfig=kubeconfig_path,
                namespace="monitoring",
                config_path=infra_asset_path(
                    workspace_root,
                    component="ipmi_exporter",
                    filename="config.yaml",
                ),
            )
        )

    # ------------------------------------------------------------
    # Prometheus Pushgateway
    # ------------------------------------------------------------
    if selection.components is None or "prometheus-pushgateway" in selection.components:
        components.append(
            PrometheusPushgatewayComponent(
                values_path=infra_asset_path(
                    workspace_root,
                    component="prometheus_pushgateway",
                    filename="values.yaml",
                ),
                assets_dir=infra_asset_path(
                    workspace_root,
                    component="prometheus_pushgateway",
                ),
                kubeconfig=kubeconfig_path,
            )
        )
    if selection.components is None or "minio" in selection.components:
        components.append(
            MinIOComponent(
                kubeconfig=kubeconfig_path,
                assets_dir=infra_asset_path(workspace_root, "minio"),
                values_path=infra_asset_path(workspace_root, "minio", "values.yaml"),
                enable_argocd=False,
            )
        )

    if monitoring_cfg and monitoring_cfg.thanos:
        if selection.components is None or "thanos" in selection.components:
            components.append(
                ThanosComponent(
                    kubeconfig=kubeconfig_path,
                    assets_dir=infra_asset_path(workspace_root, "thanos"),
                    values_path=infra_asset_path(workspace_root, "thanos", "values.yaml"),
                    s3_bucket=monitoring_cfg.thanos.bucket,
                    s3_endpoint=monitoring_cfg.thanos.endpoint,
                    s3_access_key=monitoring_cfg.thanos.access_key,
                    s3_secret_key=monitoring_cfg.thanos.secret_key,
                    enable_argocd=False,
                )
            )

    if selection.components is None or "opensearch" in selection.components:
        components.append(
            OpenSearchComponent(
                kubeconfig=kubeconfig_path,
                assets_dir=infra_asset_path(workspace_root, "opensearch"),
                values_path=infra_asset_path(workspace_root, "opensearch", "values.yaml"),
                enable_argocd=False,  # flip to True when ready
            )
        )
    return components
