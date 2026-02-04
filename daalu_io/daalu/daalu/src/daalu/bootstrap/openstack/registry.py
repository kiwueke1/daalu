# src/daalu/bootstrap/openstack/registry.py

from pathlib import Path
from typing import List

from daalu.bootstrap.openstack.models import OpenStackSelection
from daalu.bootstrap.openstack.components.memcached import MemcachedComponent
from daalu.bootstrap.infrastructure.utils.assets import infra_asset_path
from daalu.bootstrap.openstack.components.keystone.keystone import KeystoneComponent
from daalu.bootstrap.shared.keycloak.models import (
    KeycloakIAMConfig,
    KeycloakAdminAuth,
    KeycloakRealmSpec,
    KeycloakClientSpec,
    KeycloakDomainSpec,
)


def build_openstack_components(
    *,
    selection: OpenStackSelection,
    workspace_root: Path,
    kubeconfig_path: str,
    cfg,
):
    components: List = []
    secrets_path = workspace_root / "cloud-config" / "secrets.yaml"


    # ------------------------------------------------------------
    # Resolve Keycloak IAM config ONCE (shared by all components)
    # ------------------------------------------------------------
    keycloak_cfg = None
    openstack_cfg = getattr(cfg, "openstack", None)

    if getattr(cfg, "keycloak", None) and getattr(cfg.keycloak, "openstack", None):
        kc = cfg.keycloak.openstack

        keycloak_cfg = KeycloakIAMConfig(
            k8s_namespace="openstack",
            oauth2_proxy_ssl_insecure_skip_verify=kc.oauth2_proxy_ssl_insecure_skip_verify,
            oidc_issuer_url=kc.oidc_issuer_url,
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
            domains=[
                KeycloakDomainSpec(
                    name=d.name,
                    label=d.label,
                    keycloak_realm=d.keycloak_realm,
                    totp_default_action=d.totp_default_action,
                    client=KeycloakClientSpec(**d.client.model_dump()),
                )
                for d in kc.domains
            ],
        )

    if selection.components is None or "memcached" in selection.components:
        components.append(
            MemcachedComponent(
                kubeconfig=kubeconfig_path,
                assets_dir=infra_asset_path(workspace_root, "memcached"),
                values_path=infra_asset_path(
                    workspace_root, "memcached", "values.yaml"
                ),
                enable_argocd=True,
            )
        )

    # ----------------------------
    # Keystone
    # ----------------------------
    if selection.components is None or "keystone" in selection.components:
        components.append(
            KeystoneComponent(
                kubeconfig=kubeconfig_path,
                namespace="openstack",
                assets_dir=infra_asset_path(workspace_root, "keystone"),
                values_path=infra_asset_path(
                    workspace_root, "keystone", "values.yaml"
                ),
                secrets_path=workspace_root / "cloud-config" / "secrets.yaml",
                keycloak_config=keycloak_cfg,
                github_token=cfg.keycloak.openstack.github_token,
            )
        )

    return components
