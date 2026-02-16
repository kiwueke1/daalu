# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/monitoring/models.py

from __future__ import annotations

from pydantic import BaseModel, HttpUrl
from typing import List
from dataclasses import dataclass
from typing import Optional, Set


@dataclass
class MonitoringSelection:
    components: Optional[Set[str]] = None


class KeycloakMonitoringConfig(BaseModel):
    """
    Keycloak config used by monitoring stack (Grafana OIDC).
    This DOES NOT provision Keycloak.
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

    # Grafana OAuth settings
    grafana_root_url: HttpUrl
    grafana_redirect_uris: List[str]

    model_config = {
        "extra": "forbid"
    }
    
def parse_monitoring_flag(flag: Optional[str]) -> MonitoringSelection:
    """
    Reuses --infra flag semantics for monitoring.
    """
    if not flag:
        return MonitoringSelection(components=None)

    items = {i.strip() for i in flag.split(",") if i.strip()}
    if "all" in items:
        return MonitoringSelection(components=None)

    return MonitoringSelection(components=items)
