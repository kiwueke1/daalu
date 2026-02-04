# src/daalu/bootstrap/infrastructure/components/keystone/keystone.py

from pathlib import Path
import time
import json
import requests
from typing import Any

from daalu.bootstrap.engine.component import InfraComponent
from daalu.bootstrap.iam.keycloak import KeycloakIAMManager
from daalu.utils.serialize import to_jsonable
from daalu.bootstrap.shared.keycloak.models import (
    KeycloakRealmSpec,
    KeycloakDomainSpec,
    KeycloakClientSpec,
    KeycloakAdminAuth,
)
from daalu.bootstrap.shared.keycloak.iam import (
    KeycloakIAMManager,
    KeycloakIAMConfig,
)
#from daalu.bootstrap.shared.secrets.manager import SecretsManager
from daalu.bootstrap.openstack.endpoints import (
    OpenStackHelmEndpoints,
)
from daalu.utils.helpers import build_openstack_endpoints
from daalu.bootstrap.openstack.rabbitmq import RabbitMQServiceManager



class KeystoneComponent(InfraComponent):
    """
    Daalu Keystone component

    Mirrors exactly:
      roles/keystone/tasks/main.yml
      roles/keystone/tasks/argocd_onboard.yml
    """

    # -------------------------------------------------
    # Constructor
    # -------------------------------------------------
    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        keycloak_config: KeycloakIAMConfig,
        github_token: str,
        secrets_path: Path,
        cluster_name: str = "default", 
    ):
        super().__init__(
            name="keystone",
            repo_name="local",
            repo_url="",
            chart="keystone",
            version=None,
            namespace=namespace,
            release_name="keystone",
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/keystone"),
            kubeconfig=kubeconfig,
            istio_enabled=True,
            istio_host="identity.daalu.io",
            istio_service="keystone-api",
            istio_service_namespace="openstack",
            istio_service_port=5000,
            istio_expected_status=300,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self.keycloak_config = keycloak_config
        self.github_token = github_token

        self._iam: KeycloakIAMManager | None = None

        self.wait_for_pods = True
        self.min_running_pods = 1

        self.secrets_path = secrets_path
        self.cluster_name = cluster_name


        self.keycloak_cfg = keycloak_config
        self._iam = None

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------
    def assets_dir(self) -> Path:
        return self._assets_dir


    def values(self) -> dict:
        base = self.load_values_file(self.values_path)

        if not hasattr(self, "_computed_endpoints"):
            raise RuntimeError("OpenStack endpoints not computed yet")

        base["endpoints"] = self._computed_endpoints
        return base

    # -------------------------------------------------
    # 1) Create Keycloak realms
    # -------------------------------------------------
    def _ensure_keycloak_realm(self):
        print("[keystone] Ensuring Keycloak realm...")
        self._iam.ensure_realm()
        print("[keystone] Keycloak realm ensured ✓")

    def _create_keycloak_realms(self):
        print("[keystone] Creating Keycloak realms...")
        for domain in self._iter_domains():
            self._iam.ensure_realm(
                realm=domain.keycloak_realm,
                display_name=domain.label,
            )
        print("[keystone] Keycloak realms created ✓")

    def _ensure_iam(self):
        if self._iam is not None:
            return

        if not self.keycloak_cfg:
            raise RuntimeError(
                "Keycloak IAM requested but keycloak config is missing"
            )

        # At this point keycloak_cfg is already a KeycloakIAMConfig
        if not isinstance(self.keycloak_cfg, KeycloakIAMConfig):
            raise TypeError(
                f"Expected KeycloakIAMConfig, got {type(self.keycloak_cfg)}"
            )

        self._iam = KeycloakIAMManager(self.keycloak_cfg)



    # -------------------------------------------------
    # 2) Setup MFA required actions
    # -------------------------------------------------
    def _setup_keycloak_required_actions(self):
        print("[keystone] Configuring Keycloak required actions (MFA)...")
        for domain in self._iter_domains():
            self._iam.ensure_required_action(
                realm=domain.keycloak_realm,
                alias="CONFIGURE_TOTP",
                name="Configure OTP",
                enabled=True,
                default_action=domain.totp_default_action,
            )
        print("[keystone] Required actions configured ✓")




    # -------------------------------------------------
    # 3) Create OpenID metadata ConfigMap
    # -------------------------------------------------
    def _create_openid_metadata_configmap(self, kubectl):
        print("[keystone] Creating OpenID metadata ConfigMap...")
        template = self.assets_dir() / "templates/configmap-openid-metadata.yml.j2"
        kubectl.apply_template(
            template=template,
            namespace=self.namespace,
        )
        print("[keystone] OpenID metadata ConfigMap created ✓")

    # -------------------------------------------------
    # 4) Create Keycloak clients
    # -------------------------------------------------
    def _create_keycloak_client(self):
        print("[keystone] Ensuring Keystone Keycloak client...")
        client = next(
            c for c in self.keycloak_config.clients if c.id == "keystone"
        )

        client_uuid = self._iam.ensure_client(client)
        print("[keystone] Keystone client ensured ✓")

        return client_uuid


    # -------------------------------------------------
    # 5) Deploy Keystone Helm chart
    # -------------------------------------------------
    def _deploy_keystone_helm(self):
        print("[keystone] Deploying Keystone Helm chart...")
        self.helm_install(values=self.values())
        print("[keystone] Keystone Helm deployed ✓")


    # -------------------------------------------------
    # 8) Wait for Keystone API ready
    # -------------------------------------------------
    def _wait_for_keystone_ready(self, kubectl):
        print("[keystone] Waiting for keystone-api deployment...")
        kubectl.wait_for_deployment_ready(
            name="keystone-api",
            namespace=self.namespace,
            timeout=600,
        )
        print("[keystone] Keystone API ready ✓")

    # -------------------------------------------------
    # 9) Create Keystone domains
    # -------------------------------------------------
    def _create_keystone_domains(self):
        print("[keystone] Creating Keystone domains...")
        self._ensure_iam()

        for domain in self._iter_domains():
            print(f"domain is {domain}")
            self._ensure_single_keystone_domain(domain)

        print("[keystone] Keystone domains created ✓")


    def _ensure_single_keystone_domain(self, domain: KeycloakDomainSpec):
        """
        A Keystone domain is ensured by:
        - ensuring Keycloak realm exists (already handled by IAM)
        - ensuring Keystone domain exists
        - wiring identity provider later
        """

        # 1. Keystone domain itself
        self._ensure_domain_in_keystone(domain)

        # 2. Ensure Keycloak client exists for this domain
        self._iam.ensure_client(domain.client)


    def _ensure_domain_in_keystone(self, domain: KeycloakDomainSpec):
        """
        Verify that the Keystone domain exists.
        Domains are created declaratively via Helm (ks_domains).
        """

        pod = self._get_keystone_api_pod()

        cmd = (
            f"exec {pod} -n {self.namespace} -- "
            f"openstack domain show {domain.name} -f json"
        )

        rc, out, err = self.kubectl._run(cmd)

        if rc != 0:
            raise RuntimeError(
                f"Keystone domain '{domain.name}' not found. "
                f"Expected it to be created via Helm ks_domains.\n"
                f"{err or out}"
            )

        # Optional debug / logging
        try:
            data = json.loads(out)
            print(f"[keystone] Domain verified: {data.get('name')}")
        except Exception:
            print(f"[keystone] Domain '{domain.name}' verified")


    def _exec_keystone(self, cmd: list[str]):
        """
        Execute a command inside the keystone-api pod.
        """
        pod = self._get_keystone_api_pod()

        return self.kubectl.exec(
            pod=pod,
            namespace=self.namespace,
            command=cmd,
            container="keystone-api",
            check=True,
        )

    def _get_keystone_api_pod(self) -> str:
        pods = self.kubectl.get_pods(self.namespace)

        for pod in pods:
            labels = pod.get("metadata", {}).get("labels", {})
            if (
                labels.get("application") == "keystone"
                and labels.get("component") == "api"
            ):
                return pod["metadata"]["name"]

        raise RuntimeError("No keystone-api pod found")


    # -------------------------------------------------
    # 10) Create identity providers
    # -------------------------------------------------
    def _create_identity_providers(self):
        print("[keystone] Creating identity providers...")
        for domain in self._iter_domains():
            self._iam.ensure_identity_provider(domain)
        print("[keystone] Identity providers created ✓")


    # -------------------------------------------------
    # 11) Create federation mappings
    # -------------------------------------------------
    def _create_federation_mappings(self):
        print("[keystone] Creating federation mappings...")
        for domain in self._iter_domains():
            self._iam.ensure_federation_mapping(domain)
        print("[keystone] Federation mappings created ✓")


    # -------------------------------------------------
    # 12) Create federation protocols
    # -------------------------------------------------
    def _create_federation_protocols(self):
        print("[keystone] Creating federation protocols...")
        for domain in self._iter_domains():
            self._iam.ensure_federation_protocol(domain)
        print("[keystone] Federation protocols created ✓")


    # -------------------------------------------------
    # 13) Argo CD onboarding
    # -------------------------------------------------
    def _onboard_argocd(self, kubectl):
        print("[keystone] Checking Argo CD onboarding...")
        apps = kubectl.get_argocd_apps()

        if "keystone" in [a.lower() for a in apps]:
            print("[keystone] Already onboarded to Argo CD ✓")
            return

        print("[keystone] Onboarding Keystone to Argo CD...")
        url = (
            "https://api.github.com/repos/kiwueke1/"
            "argocd-infrastructure-app/contents/apps/openstack/keystone/keystone.yaml"
        )

        r = requests.get(
            url,
            headers={
                "Accept": "application/vnd.github.v3.raw",
                "Authorization": f"token {self.github_token}",
            },
            timeout=10,
        )
        r.raise_for_status()

        kubectl.apply_yaml(
            content=r.text,
            kubeconfig="/etc/kubernetes/admin.conf",
        )
        print("[keystone] Keystone onboarded to Argo CD ✓")

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------

    def pre_install_1(self, kubectl):
        print("[keystone] Starting pre-install...")

        print("[keystone] Keycloak config:")
        print(json.dumps(to_jsonable(self.keycloak_config), indent=2))

        # ----------------------------
        # Keycloak IAM bootstrap (PRE-HELM)
        # ----------------------------
        for iam in self._iter_iam_managers():
            iam.run(kubectl)

        print("[keystone] pre-install complete ✓")


    def pre_install(self, kubectl):
        print("[keystone] Starting pre-install...")

        # -------------------------------------------------
        # 1) Generate OpenStack Helm endpoints (DB, Rabbit, Cache)
        # -------------------------------------------------
        print("[keystone] Building OpenStack Helm endpoints...")

        rmq = RabbitMQServiceManager(
            kubectl=kubectl,
            namespace=self.namespace,
        )

        rmq.ensure_cluster("keystone")

        self._computed_endpoints = build_openstack_endpoints(
            kubectl=kubectl,
            secrets_path=self.secrets_path,
            namespace=self.namespace,
            region_name="RegionOne",
            keystone_public_host=str(self.keycloak_config.admin.base_url)
            .replace("https://", "")
            .rstrip("/"),
            service="keystone",
        )

        print("[keystone] OpenStack endpoints ready ✓")

        # -------------------------------------------------
        # DEBUG 1: Dump computed OpenStack endpoints
        # -------------------------------------------------
        print("[keystone][DEBUG] Computed OpenStack Helm endpoints:")
        print(
            json.dumps(
                to_jsonable(self._computed_endpoints),
                indent=2,
                sort_keys=True,
                default=str,
            )
        )

        # -------------------------------------------------
        # DEBUG 2: Dump FINAL Helm values (values.yaml + endpoints)
        # -------------------------------------------------
        values = self.values()

        # If your Helm engine merges endpoints later, expose them explicitly
        values_with_endpoints = dict(values)
        values_with_endpoints.setdefault("endpoints", {})
        values_with_endpoints["endpoints"].update(self._computed_endpoints)

        print("[keystone][DEBUG] FINAL Keystone Helm values (pre-install):")
        print(
            json.dumps(
                values_with_endpoints,
                indent=2,
                sort_keys=True,
                default=str,
            )
        )

        # -------------------------------------------------
        # DEBUG 3: Focused OpenRC / auth values (Helm Toolkit failure zone)
        # -------------------------------------------------
        print("[keystone][DEBUG] Keystone OpenRC-related values:")
        print(
            json.dumps(
                {
                    "endpoints.identity": (
                        values_with_endpoints
                        .get("endpoints", {})
                        .get("identity")
                    ),
                    "conf.keystone.auth": (
                        values_with_endpoints
                        .get("conf", {})
                        .get("keystone", {})
                        .get("auth")
                    ),
                },
                indent=2,
                default=str,
            )
        )


        # -------------------------------------------------
        # 4) Keycloak IAM bootstrap (PRE-HELM)
        # -------------------------------------------------
        for iam in self._iter_iam_managers():
            iam.run(kubectl)

        print("[keystone] pre-install complete ✓")




    def post_install(self, kubectl):
        print("[keystone] Starting post-install...")
        self.kubectl = kubectl

        # Parent handles Istio + validation
        super().post_install(kubectl)

        self._wait_for_keystone_ready(kubectl)

        self._create_keystone_domains()
        self._create_identity_providers()
        self._create_federation_mappings()
        self._create_federation_protocols()

        print("[keystone] post-install complete ✓")


    # ----------------------------
    # Helper methods
    # ----------------------------
    def _iter_domains_1(self):
        """
        Normalizes Keycloak config so Keystone logic can always iterate.
        Supports:
        - single-realm mode (no domains)
        - multi-domain mode
        """
        kc = self.keycloak_config  # Keystone-level config

        if getattr(kc, "domains", None):
            return kc.domains

        # Fallback: synthesize a single domain from top-level realm
        return [
            KeycloakDomainSpec(
                name=kc.realm.realm,
                label=kc.realm.display_name,
                keycloak_realm=kc.realm.realm,
                totp_default_action=True,
                client=kc.clients[0] if kc.clients else None,
            )
        ]

    def _iter_domains(self):
        return self.keycloak_config.normalized_domains()



    def _iter_iam_managers(self):
        """
        One IAM manager per Keycloak realm.
        """
        for domain in self._iter_domains():
            yield KeycloakIAMManager(
                KeycloakIAMConfig(
                    k8s_namespace=self.keycloak_config.k8s_namespace,
                    admin=self.keycloak_config.admin,
                    realm=KeycloakRealmSpec(
                        realm=domain.keycloak_realm,
                        display_name=domain.label,
                    ),
                    clients=[domain.client] if domain.client else [],
                    oidc_issuer_url=f"{self.keycloak_config.admin.base_url}/realms/{domain.keycloak_realm}",
                    oauth2_proxy_ssl_insecure_skip_verify=
                        self.keycloak_config.oauth2_proxy_ssl_insecure_skip_verify,
                )
            )
