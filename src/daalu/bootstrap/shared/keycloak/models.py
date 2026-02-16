# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/shared/keycloak/models.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class KeycloakRealmSpec:
    realm: str
    display_name: str
    enabled: bool = True


@dataclass(frozen=True)
class KeycloakClientSpec:
    """
    Fields:
    - id: client_id in Keycloak
    - roles: client roles to ensure
    - oauth2_proxy: whether we create oauth2-proxy SecretTemplate
    - redirect_uris: used by Keycloak client and oauth2-proxy config
    - port: used only for oauth2-proxy upstream (127.0.0.1:<port>)
    - public: public vs confidential client
    - protocol: usually 'openid-connect'
    - root_url: root URL for the application (Grafana, Horizon, etc.)
    - base_url: optional base URL (Keycloak UI field)
    - secret_name: Kubernetes Secret name (if confidential)
    - namespace: namespace where Secret is created
    - default_scopes: OIDC scopes to attach
    """
    id: str

    roles: List[str] = field(default_factory=list)

    oauth2_proxy: bool = False
    redirect_uris: List[str] = field(default_factory=list)
    port: Optional[int] = None

    public: bool = False
    protocol: str = "openid-connect"

    root_url: Optional[str] = None
    base_url: Optional[str] = None

    secret_name: Optional[str] = None
    namespace: Optional[str] = None

    default_scopes: List[str] = field(
        default_factory=lambda: [
            "openid",
            "profile",
            "email",
            "roles",
        ]
    )


@dataclass(frozen=True)
class KeycloakIAMSpec:
    admin: KeycloakAdminAuth
    realm: KeycloakRealmSpec
    clients: List[KeycloakClientSpec]


@dataclass(frozen=True)
class KeycloakAdminAuth:
    base_url: str               # e.g. https://keycloak.example.com
    admin_realm: str            # e.g. master
    admin_client_id: str        # e.g. admin-cli
    username: str               # e.g. admin
    password: str               # e.g. admin password
    verify_tls: bool = True     # False for self-signed

@dataclass(frozen=True)
class KeycloakIAMConfig:
    # Kubernetes namespace where the monitoring secrets live (e.g. "monitoring")
    k8s_namespace: str

    # Keycloak admin login + API endpoint
    admin: KeycloakAdminAuth

    # Realm to manage
    realm: KeycloakRealmSpec

    # Clients to ensure + their roles/oauth2-proxy config
    clients: List[KeycloakClientSpec]

    # Used in oauth2-proxy SecretTemplate
    # e.g. https://keycloak.example.com/realms/atmosphere
    oidc_issuer_url: str

    # Optional / defaulted fields MUST come last
    domains: Optional[List["KeycloakDomainSpec"]] = None
    oauth2_proxy_ssl_insecure_skip_verify: bool = False


    def normalized_domains(self) -> List[KeycloakDomainSpec]:
        """
        - if domains are provided, use them
        - otherwise synthesize keystone domain from keycloak realm
        """
        if self.domains:
            return self.domains

        return [
            KeycloakDomainSpec(
                name=self.realm.realm,
                label=self.realm.display_name,
                keycloak_realm=self.realm.realm,
                totp_default_action=True,
                client=next(
                    (c for c in self.clients if c.id == "keystone"),
                    None,
                ),
            )
        ]



@dataclass(frozen=True)
class KeycloakDomainSpec:
    name: str                  # keystone domain name
    label: str                 # human readable
    keycloak_realm: str        # realm name in keycloak
    totp_default_action: bool = True
    client: KeycloakClientSpec = None