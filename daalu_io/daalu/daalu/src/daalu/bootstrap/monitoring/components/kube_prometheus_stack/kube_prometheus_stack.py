# src/daalu/bootstrap/monitoring/components/kube_prometheus_stack.py

from pathlib import Path
import base64
import json
import time
import requests
from dataclasses import asdict


from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.shared.keycloak.models import KeycloakIAMConfig
from daalu.bootstrap.iam.keycloak import KeycloakIAMManager
from daalu.bootstrap.shared.keycloak.models import KeycloakIAMConfig
from daalu.utils.serialize import to_jsonable



class KubePrometheusStackComponent(InfraComponent):

    # ----------------------------
    # Constructor (unchanged API)
    # ----------------------------
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
        self.keycloak_config = keycloak_config
        self._iam: KeycloakIAMManager | None = None

        self.wait_for_pods = True
        self.min_running_pods = 3

    # ----------------------------
    # Helpers
    # ----------------------------
    def assets_dir(self) -> Path:
        return self._assets_dir

    def values(self) -> dict:
        return self.load_values_file(self.values_path)

    def _install_crds(self, kubectl):
        print("[kube-prometheus] Installing CRDs...")
        try:
            local = self.assets_dir() / "crds"
            remote = "/tmp/daalu/kube-prometheus-stack/crds"

            kubectl.ssh.run(f"mkdir -p {remote}", sudo=True)

            for crd in sorted(local.glob("*.yaml")):
                path = f"{remote}/{crd.name}"
                kubectl.ssh.put_file(crd, path, sudo=True)
                kubectl.apply_file_server_side(path)

            print("[kube-prometheus] CRDs installed ✓")
        except Exception as e:
            print(f"[kube-prometheus] CRDs FAILED ✗: {e}")
            raise

    def _read_etcd_certs(self, kubectl) -> dict:
        print("[kube-prometheus] Reading etcd certificates...")
        certs = {}
        paths = {
            "ca.crt": "/etc/kubernetes/pki/etcd/ca.crt",
            "healthcheck-client.crt": "/etc/kubernetes/pki/etcd/healthcheck-client.crt",
            "healthcheck-client.key": "/etc/kubernetes/pki/etcd/healthcheck-client.key",
        }

        for name, path in paths.items():
            rc, out, err = kubectl.ssh.run(f"cat {path}", sudo=True)
            if rc != 0:
                raise RuntimeError(err)
            certs[name] = base64.b64encode(out.encode()).decode()

        print("[kube-prometheus] etcd certs read ✓")
        return certs

    def _create_etcd_secret(self, kubectl, certs: dict):
        print("[kube-prometheus] Creating etcd TLS secret...")
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "kube-prometheus-stack-etcd-client-cert",
                    "namespace": self.namespace,
                },
                "data": certs,
            }
        ])
        print("[kube-prometheus] etcd secret created ✓")

    def _wait_for_keycloak(self, kubectl):
        print("[keycloak] Waiting for Keycloak StatefulSet...")
        kubectl.wait_for_statefulset_ready(
            name="keycloak",
            namespace="auth-system",
            retries=120,
            delay=5,
        )
        print("[keycloak] Keycloak ready ✓")

    def _create_grafana_keycloak_secret(self, kubectl):
        """
        Creates the Secret that Grafana mounts at /etc/secrets/keycloak
        Name MUST be: grafana-keycloak-client
        """

        print("[kube-prometheus] Creating Grafana Keycloak secret...")
        iam = self._iam
        if not iam:
            raise RuntimeError("Keycloak IAM not initialized before Grafana secret creation")

        cfg = self.keycloak_config
        grafana = next(c for c in cfg.clients if c.id == "grafana")

        # 1) Ensure client exists in Keycloak
        client_uuid = iam.ensure_client(grafana)

        # 2) Fetch client secret from Keycloak
        client_secret = iam.get_client_secret(client_uuid=client_uuid)

        # 3) Create Kubernetes Secret (THIS WAS MISSING)
        kubectl.apply_objects([
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "grafana-keycloak-client",
                    "namespace": self.namespace,
                },
                "type": "Opaque",
                "stringData": {
                    "client_id": grafana.id,
                    "client_secret": client_secret,
                    "issuer_url": (
                        f"{str(cfg.admin.base_url).rstrip('/')}/realms/{cfg.realm.realm}"
                        #f"/realms/{cfg.realm.realm}"
                    ),
                },
            }
        ])

        print("[kube-prometheus] Grafana Keycloak secret created ✓")


    def _debug_keycloak_config(self):
        print("[keycloak] Config:")
        print(json.dumps(to_jsonable(self.keycloak_config), indent=2))

    def _ensure_keycloak(self):
        if not self.keycloak_config:
            return

        if self._iam:
            # Idempotency: already initialized
            return

        iam = KeycloakIAMManager(config=self.keycloak_config)

        iam.login()
        iam.ensure_realm()

        for client in self.keycloak_config.clients:
            client_uuid = iam.ensure_client(client)
            iam.ensure_client_roles(
                client_uuid=client_uuid,
                roles=client.roles,
            )

        self._iam = iam



    def _create_oauth2_proxy_secrets(self, kubectl):
        for client in self.keycloak_config.clients:
            if not client.oauth2_proxy:
                continue

            kubectl.apply_objects([{
                "apiVersion": "secretgen.carvel.dev/v1alpha1",
                "kind": "SecretTemplate",
                "metadata": {
                    "name": f"{self.release_name}-{client.id}-oauth2-proxy",
                    "namespace": self.namespace,
                },
                "spec": {
                    # REQUIRED by SecretTemplate CRD
                    "inputResources": [],

                    "template": {
                        "stringData": {
                            "OAUTH2_PROXY_PROVIDER": "keycloak-oidc",
                            "OAUTH2_PROXY_CLIENT_ID": client.id,
                            "OAUTH2_PROXY_REDIRECT_URL": client.redirect_uris[0],
                        }
                    },
                },
            }])

    def pre_install(self, kubectl):
        # ------------------------------------------------
        # 1) CRDs
        # ------------------------------------------------
        self._install_crds(kubectl)

        # ------------------------------------------------
        # 2) etcd TLS certs + secret
        # ------------------------------------------------
        certs = self._read_etcd_certs(kubectl)
        self._create_etcd_secret(kubectl, certs)

        # ------------------------------------------------
        # 3) Keycloak + OAuth (OPTIONAL)
        # ------------------------------------------------
        if not self.keycloak_config:
            print("[kube-prometheus] Keycloak config not provided — skipping IAM setup")
            #kubectl.logger.info(
            #    "[kube-prometheus] Keycloak config not provided — skipping IAM setup"
            #)
            return

        self._wait_for_keycloak(kubectl)

        # This MUST log unconditionally once Keycloak is enabled
        self._debug_keycloak_config()

        # Realm + clients in Keycloak
        self._ensure_keycloak()

        
        self._create_grafana_keycloak_secret(kubectl)

        # OAuth2-proxy / SecretTemplate resources
        self._create_oauth2_proxy_secrets(kubectl)


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
                ],
                server_side=True,
            )

        super().post_install(kubectl)
