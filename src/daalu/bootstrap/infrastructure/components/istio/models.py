# src/daalu/bootstrap/infrastructure/components/istio/models.py

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Dict


# -----------------------------
# Gateway
# -----------------------------

@dataclass
class IstioGatewayTLS:
    mode: str
    credentialName: str


@dataclass
class IstioGatewayConfig:
    name: str
    namespace: str
    selector: Dict[str, str]
    tls: IstioGatewayTLS
    http_redirect_port_80_to_443: bool = True


# -----------------------------
# Service / Traffic
# -----------------------------

@dataclass
class IstioServiceConfig:
    name: str
    port: int
    subset: Optional[str] = None


@dataclass
class IstioDestinationRuleConfig:
    trafficPolicy: Dict


@dataclass
class IstioApplication:
    name: str
    hostnames: List[str]
    traffic_namespace: str
    original_svc_namespace: str
    gateway: IstioGatewayConfig
    service: IstioServiceConfig
    destinationrule: IstioDestinationRuleConfig


# -----------------------------
# Root config
# -----------------------------

@dataclass
class IstioTrafficConfig:
    applications: List[IstioApplication]
