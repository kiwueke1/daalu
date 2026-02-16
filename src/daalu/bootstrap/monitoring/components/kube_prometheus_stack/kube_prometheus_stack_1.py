# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/monitoring/components/kube_prometheus_stack.py

from pathlib import Path
import base64
import json
import time
import requests

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.shared.keycloak.models import KeycloakIAMConfig
from daalu.bootstrap.iam.keycloak import KeycloakIAMManager
from daalu.bootstrap.shared.keycloak.models import KeycloakIAMConfig


class KubePrometheusStackComponent(InfraComponent):
    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "monitoring",
        keycloak_config: KeycloakIAMConfig | None = None,
    ):
        super().__init__(
            name="kube-prometheus-stack",
            repo_name="local",
            repo_url="",
            chart="kube-prometheus-stack",
            version=None,
            namespace=namespace,
            release_name="kube-prometheus-stack",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/kube-prometheus-stack"),
            kubeconfig=kubeconfig,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir

        self.wait_for_pods = True
        self.min_running_pods = 3
        self.enable_argocd = False

        self.keycloak_config = keycloak_config


    def assets_dir(self) -> Path:
        return self._assets_dir

    def values(self) -> dict:
        return self.load_values_file(self.values_path)

    def pre_install(self, kubectl):
        # ================================================================
        # 1) Install Prometheus Operator CRDs
            # ================================================================
        local_crds_dir = self.assets_dir() / "crds"
        remote_crds_dir = "/tmp/daalu/kube-prometheus-stack/crds"

        # Ensure remote directory exists
        kubectl.ssh.run(f"mkdir -p {remote_crds_dir}", sudo=True)

        for crd in sorted(local_crds_dir.glob("*.yaml")):
            remote_path = f"{remote_crds_dir}/{crd.name}"

            # Upload with sudo-safe method
            kubectl.ssh.put_file(crd, remote_path, sudo=True)

            # CRDs: server-side apply to avoid annotation size blowups
            kubectl.apply_file_server_side(remote_path)

        # ================================================================
        # 2) Create etcd TLS secret (for Prometheus scraping)
        # ================================================================
        etcd_paths = {
            "ca.crt": "/etc/kubernetes/pki/etcd/ca.crt",
            "healthcheck-client.crt": "/etc/kubernetes/pki/etcd/healthcheck-client.crt",
            "healthcheck-client.key": "/etc/kubernetes/pki/etcd/healthcheck-client.key",
        }

        data = {}
        for key, path in etcd_paths.items():
            rc, out, err = kubectl.ssh.run(f"cat {path}", sudo=True)
            if rc != 0:
                raise RuntimeError(f"Failed to read {path}: {err}")
            data[key] = base64.b64encode(out.encode()).decode()

        kubectl.apply_objects(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": self.namespace},
                },
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": "kube-prometheus-stack-etcd-client-cert",
                        "namespace": self.namespace,
                    },
                    "data": data,
                },
            ]
        )

        # ------------------------------------------------
        # 3) Keycloak + OAuth2 (OPTIONAL)
        # ------------------------------------------------
        if not self.keycloak_config:
            return

        cfg = self.keycloak_config

        iam = KeycloakIAMManager(config=cfg)
        iam.login()
        iam.ensure_realm()

        # Example: ensure grafana client
        grafana = next((c for c in cfg.clients if c.id == "grafana"), None)
        if grafana:
            client_uuid = iam.ensure_client(grafana)
            iam.ensure_client_roles(client_uuid=client_uuid, roles=grafana.roles)

            if not grafana.public:
                secret = iam.get_client_secret(client_uuid=client_uuid)

                # Write Secret into Kubernetes for grafana values to reference
                # (You can standardize name/namespace, or use grafana.secret_name/namespace)
                secret_name = grafana.secret_name or "grafana-keycloak-client"
                namespace = grafana.namespace or self.namespace

                kubectl.apply_objects(
                    [
                        {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "metadata": {"name": secret_name, "namespace": namespace},
                            "type": "Opaque",
                            "stringData": {
                                "client_id": grafana.id,
                                "client_secret": secret,
                                "issuer_url": f"{cfg.admin.base_url.rstrip('/')}/realms/{cfg.realm.realm}",
                            },
                        }
                    ]
                )


        # --- Create realm (idempotent) ---
        realm_url = f"{cfg['server_url']}/admin/realms/{cfg['realm']}"
        r = requests.get(realm_url, headers=headers, verify=cfg.get("validate_certs", True))
        if r.status_code == 404:
            create_realm = {
                "realm": cfg["realm"],
                "enabled": True,
                "displayName": cfg["realm_name"],
            }
            requests.post(
                f"{cfg['server_url']}/admin/realms",
                headers=headers,
                json=create_realm,
                verify=cfg.get("validate_certs", True),
            ).raise_for_status()

        # --- Create OAuth clients ---
        for client in cfg["clients"]:
            clients_url = f"{cfg['server_url']}/admin/realms/{cfg['realm']}/clients"
            r = requests.get(clients_url, headers=headers, verify=cfg.get("validate_certs", True))
            r.raise_for_status()

            if any(c["clientId"] == client["id"] for c in r.json()):
                continue

            client_payload = {
                "clientId": client["id"],
                "enabled": True,
                "protocol": "openid-connect",
                "redirectUris": client["redirect_uris"],
                "publicClient": False,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": True,
            }

            requests.post(
                clients_url,
                headers=headers,
                json=client_payload,
                verify=cfg.get("validate_certs", True),
            ).raise_for_status()

        # --- Create client secrets + Kubernetes SecretTemplates ---
        for client in cfg["clients"]:
            secret_name = f"{self.release_name}-{client['id']}-client-secret"

            kubectl.apply_objects(
                {
                    "apiVersion": "secretgen.k14s.io/v1alpha1",
                    "kind": "Password",
                    "metadata": {
                        "name": secret_name,
                        "namespace": self.namespace,
                    },
                    "spec": {"length": 64},
                }
            )

        # --- OAuth2 proxy config secrets ---
        for client in cfg["clients"]:
            if not client.get("oauth2_proxy"):
                continue

            kubectl.apply_objects(
                {
                    "apiVersion": "secretgen.carvel.dev/v1alpha1",
                    "kind": "SecretTemplate",
                    "metadata": {
                        "name": f"{self.release_name}-{client['id']}-oauth2-proxy",
                        "namespace": self.namespace,
                    },
                    "spec": {
                        "template": {
                            "stringData": {
                                "OAUTH2_PROXY_PROVIDER": "keycloak-oidc",
                                "OAUTH2_PROXY_CLIENT_ID": client["id"],
                                "OAUTH2_PROXY_OIDC_ISSUER_URL": (
                                    f"{cfg['server_url']}/realms/{cfg['realm']}"
                                ),
                                "OAUTH2_PROXY_REDIRECT_URL": client["redirect_uris"][0],
                                "OAUTH2_PROXY_ALLOWED_ROLE": f"{client['id']}:{client['roles'][0]}",
                            }
                        }
                    },
                }
            )

    def post_install(self, kubectl):
        dashboards_dir = self.assets_dir() / "dashboards"

        for dashboard in dashboards_dir.glob("*.json"):
            kubectl.apply_objects(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {
                            "name": f"kube-prometheus-stack-dashboard-{dashboard.stem}",
                            "namespace": self.namespace,
                            "labels": {"grafana_dashboard": "1"},
                        },
                        "data": {
                            f"{dashboard.name}": dashboard.read_text()
                        },
                    }
                ]
            )

        super().post_install(kubectl)
