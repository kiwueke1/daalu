# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/models.py

from __future__ import annotations

from pydantic import BaseModel, HttpUrl
from typing import List
from dataclasses import dataclass
from typing import List, Optional, Set


@dataclass
class OpenStackSelection:
    components: Optional[Set[str]] = None

class KeycloakDomainClientConfig(BaseModel):
    id: str

    # These MUST be accepted because YAML provides them
    roles: List[str] = []
    oauth2_proxy: bool = False

    redirect_uris: List[str] = []

    model_config = {
        "extra": "forbid",
    }

class KeycloakDomainConfig(BaseModel):
    name: str
    label: str
    keycloak_realm: str
    totp_default_action: bool = True
    client: Optional[KeycloakDomainClientConfig] = None

    model_config = {
        "extra": "forbid",
    }

class KeycloakOpenstackConfig(BaseModel):
    """
    Keycloak config used by openstack 
    """

    # Admin access (for realm/client inspection only, not creation)
    base_url: HttpUrl
    admin_realm: str
    admin_client_id: str
    username: str
    password: str
    verify_tls: bool = True

    # Realm Grafana authenticates against
    realm: str
    display_name: str

    domains: Optional[List[KeycloakDomainConfig]] = None

    # Github
    github_token: str

    # Grafana OAuth settings
    grafana_root_url: HttpUrl
    grafana_redirect_uris: List[str]
    oauth2_proxy_ssl_insecure_skip_verify: bool = False

    oidc_issuer_url: str

    model_config = {
        "extra": "forbid"
    }
def parse_openstack_flag(flag: Optional[str]) -> OpenStackSelection:
    if not flag:
        return OpenStackSelection(components=None)

    items = {i.strip().lower() for i in flag.split(",") if i.strip()}
    if "all" in items:
        return OpenStackSelection(components=None)

    return OpenStackSelection(components=items)
