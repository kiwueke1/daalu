# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/components/istio/traffic.py

from pathlib import Path
import yaml

from daalu.bootstrap.engine.component import InfraComponent
from .models import (
    IstioTrafficConfig,
    IstioApplication,
    IstioGatewayConfig,
    IstioGatewayTLS,
    IstioServiceConfig,
    IstioDestinationRuleConfig,
)


class IstioTrafficComponent(InfraComponent):
    def __init__(self, *, config_path: Path, kubeconfig: str):
        super().__init__(
            name="istio-traffic",
            repo_name="none",
            repo_url="",
            chart="",
            version=None,
            namespace="istio-ingress",
            release_name="istio-traffic",
            local_chart_dir=Path("/tmp"),
            remote_chart_dir=Path("/tmp"),
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self.config_path = config_path
        self.cfg = self._load_config()
        self.wait_for_pods = False

        self._values: Dict = {}

    # --------------------------------------------------
    def _load_config(self) -> IstioTrafficConfig:
        raw = yaml.safe_load(self.config_path.read_text())

        apps = []
        for a in raw["applications"]:
            gw = a["gateway"]
            gateway = IstioGatewayConfig(
                name=gw["name"],
                namespace=gw["namespace"],
                selector=gw["selector"],
                tls=IstioGatewayTLS(**gw["tls"]),
                http_redirect_port_80_to_443=gw.get(
                    "http_redirect_port_80_to_443", True
                ),
            )

            app = IstioApplication(
                name=a["name"],
                hostnames=a["hostnames"],
                traffic_namespace=a["traffic_namespace"],
                original_svc_namespace=a["original_svc_namespace"],
                gateway=gateway,
                service=IstioServiceConfig(**a["service"]),
                destinationrule=IstioDestinationRuleConfig(
                    trafficPolicy=a["destinationrule"]["trafficPolicy"]
                ),
            )
            apps.append(app)

        return IstioTrafficConfig(applications=apps)

    # --------------------------------------------------
    def post_install(self, kubectl) -> None:
        for app in self.cfg.applications:
            self._ensure_namespace(kubectl, app)
            self._ensure_gateway(kubectl, app)
            self._ensure_destination_rule(kubectl, app)
            self._ensure_virtual_service(kubectl, app)

    # --------------------------------------------------
    def _ensure_namespace(self, kubectl, app: IstioApplication):
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": app.traffic_namespace},
            }
        ])

    def _ensure_gateway(self, kubectl, app: IstioApplication):
        gw = app.gateway
        kubectl.apply_objects([
            {
                "apiVersion": "networking.istio.io/v1beta1",
                "kind": "Gateway",
                "metadata": {
                    "name": gw.name,
                    "namespace": gw.namespace,
                },
                "spec": {
                    "selector": gw.selector,
                    "servers": [
                        {
                            "port": {
                                "number": 80,
                                "name": "http",
                                "protocol": "HTTP",
                            },
                            "hosts": ["*.daalu.io", "daalu.io"],
                            "tls": {"httpsRedirect": True},
                        },
                        {
                            "port": {
                                "number": 443,
                                "name": "https",
                                "protocol": "HTTPS",
                            },
                            "hosts": ["*.daalu.io", "daalu.io"],
                            "tls": {
                                "mode": gw.tls.mode,
                                "credentialName": gw.tls.credentialName,
                            },
                        },
                    ],
                },
            }
        ])

    def _ensure_destination_rule(self, kubectl, app: IstioApplication):
        subsets = []
        if app.service.subset:
            subsets.append({
                "name": app.service.subset,
                "labels": {"version": app.service.subset},
            })

        kubectl.apply_objects([
            {
                "apiVersion": "networking.istio.io/v1beta1",
                "kind": "DestinationRule",
                "metadata": {
                    "name": f"{app.name}-dr",
                    "namespace": app.traffic_namespace,
                },
                "spec": {
                    "host": f"{app.service.name}.{app.original_svc_namespace}.svc.cluster.local",
                    "trafficPolicy": app.destinationrule.trafficPolicy,
                    "subsets": subsets,
                },
            }
        ])

    def _ensure_virtual_service(self, kubectl, app: IstioApplication):
        destination = {
            "host": f"{app.service.name}.{app.original_svc_namespace}.svc.cluster.local",
            "port": {"number": app.service.port},
        }

        if app.service.subset:
            destination["subset"] = app.service.subset

        kubectl.apply_objects([
            {
                "apiVersion": "networking.istio.io/v1beta1",
                "kind": "VirtualService",
                "metadata": {
                    "name": f"{app.name}-vs",
                    "namespace": app.traffic_namespace,
                },
                "spec": {
                    "hosts": app.hostnames,
                    "gateways": [
                        f"{app.gateway.namespace}/{app.gateway.name}"
                    ],
                    "http": [
                        {
                            "match": [{"uri": {"prefix": "/"}}],
                            "route": [{"destination": destination}],
                        }
                    ],
                },
            }
        ])
