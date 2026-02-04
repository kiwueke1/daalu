# src/daalu/bootstrap/shared/keycloak/iam.py

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import List

from daalu.bootstrap.shared.keycloak.admin import KeycloakAdmin
from daalu.bootstrap.shared.keycloak.models import (
    KeycloakAdminAuth,
    KeycloakClientSpec,
    KeycloakRealmSpec,
    KeycloakIAMConfig,
)




class KeycloakIAMManager:
    """
    Shared helper to get Ansible parity:
    - secretgen Password CRs -> Secret values
    - Keycloak realm + clients + roles
    - secretgen SecretTemplate for oauth2-proxy env vars
    """

    def __init__(self, cfg: KeycloakIAMConfig):
        self.cfg = cfg
        self.kc = KeycloakAdmin(cfg.admin)

    # ----------------------------
    # Kubernetes secretgen helpers
    # ----------------------------
    def ensure_client_secret_passwords(self, kubectl) -> None:
        """
        Equivalent to:
        - kind: Password (secretgen.k14s.io)
        - wait for ReconcileSucceeded
        """
        ns = self.cfg.k8s_namespace

        # Ensure namespace exists
        kubectl.apply_objects(
            [
                {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}},
            ]
        )

        for c in self.cfg.clients:
            name = f"{c.id}-client-secret"
            kubectl.apply_objects(
                [
                    {
                        "apiVersion": "secretgen.k14s.io/v1alpha1",
                        "kind": "Password",
                        "metadata": {"name": name, "namespace": ns},
                        "spec": {"length": 64},
                    }
                ]
            )
            # Wait until controller reconciles
            kubectl.wait_for_condition(
                api_version="secretgen.k14s.io/v1alpha1",
                kind="Password",
                name=name,
                namespace=ns,
                condition_type="ReconcileSucceeded",
                condition_status="True",
                timeout_seconds=120,
            )

    def read_client_secret(self, kubectl, *, client_id: str) -> str:
        """
        Reads the Secret created by secretgen Password controller.
        Secret name == Password name.
        Secret.data.password is base64 already; you want decoded bytes.
        """
        ns = self.cfg.k8s_namespace
        name = f"{client_id}-client-secret"

        secret = kubectl.get_object(
            api_version="v1",
            kind="Secret",
            name=name,
            namespace=ns,
        )
        b64_pw = secret["data"]["password"]
        return base64.b64decode(b64_pw).decode("utf-8", errors="replace")

    def ensure_cookie_secrets_for_oauth2(self, kubectl) -> None:
        ns = self.cfg.k8s_namespace
        for c in self.cfg.clients:
            if not c.oauth2_proxy:
                continue

            name = f"{c.id}-cookie-secret"
            kubectl.apply_objects(
                [
                    {
                        "apiVersion": "secretgen.k14s.io/v1alpha1",
                        "kind": "Password",
                        "metadata": {"name": name, "namespace": ns},
                        "spec": {"length": 32},
                    }
                ]
            )
            kubectl.wait_for_condition(
                api_version="secretgen.k14s.io/v1alpha1",
                kind="Password",
                name=name,
                namespace=ns,
                condition_type="ReconcileSucceeded",
                condition_status="True",
                timeout_seconds=120,
            )

    def ensure_oauth2_proxy_secret_templates(self, kubectl) -> None:
        """
        Equivalent to your SecretTemplate in Ansible.
        Produces Secret with env vars for oauth2-proxy sidecar.
        """
        ns = self.cfg.k8s_namespace

        for c in self.cfg.clients:
            if not c.oauth2_proxy:
                continue
            if not c.port:
                raise RuntimeError(f"oauth2_proxy client {c.id} missing port")
            if not c.redirect_uris:
                raise RuntimeError(f"oauth2_proxy client {c.id} missing redirect_uris")

            name = f"{c.id}-oauth2-proxy"

            kubectl.apply_objects(
                [
                    {
                        "apiVersion": "secretgen.carvel.dev/v1alpha1",
                        "kind": "SecretTemplate",
                        "metadata": {"name": name, "namespace": ns},
                        "spec": {
                            "inputResources": [
                                {
                                    "name": "client-secret",
                                    "ref": {
                                        "apiVersion": "v1",
                                        "kind": "Secret",
                                        "name": f"{c.id}-client-secret",
                                    },
                                },
                                {
                                    "name": "cookie-secret",
                                    "ref": {
                                        "apiVersion": "v1",
                                        "kind": "Secret",
                                        "name": f"{c.id}-cookie-secret",
                                    },
                                },
                            ],
                            "template": {
                                "stringData": {
                                    "OAUTH2_PROXY_UPSTREAMS": f"http://127.0.0.1:{c.port}",
                                    "OAUTH2_PROXY_HTTP_ADDRESS": "0.0.0.0:8081",
                                    "OAUTH2_PROXY_METRICS_ADDRESS": "0.0.0.0:8082",
                                    "OAUTH2_PROXY_EMAIL_DOMAINS": "*",
                                    "OAUTH2_PROXY_REVERSE_PROXY": "true",
                                    "OAUTH2_PROXY_SKIP_PROVIDER_BUTTON": "true",
                                    "OAUTH2_PROXY_SSL_INSECURE_SKIP_VERIFY": str(
                                        self.cfg.oauth2_proxy_ssl_insecure_skip_verify
                                    ).lower(),
                                    "OAUTH2_PROXY_PROVIDER": "keycloak-oidc",
                                    "OAUTH2_PROXY_CLIENT_ID": c.id,
                                    "OAUTH2_PROXY_REDIRECT_URL": c.redirect_uris[0],
                                    "OAUTH2_PROXY_OIDC_ISSUER_URL": self.cfg.oidc_issuer_url,
                                    # First role is treated as "allowed role" like your Ansible
                                    "OAUTH2_PROXY_ALLOWED_ROLE": f"{c.id}:{c.roles[0]}",
                                    "OAUTH2_PROXY_CODE_CHALLENGE_METHOD": "S256",
                                },
                                "data": {
                                    "OAUTH2_PROXY_COOKIE_SECRET": "$(.cookie-secret.data.password)",
                                    "OAUTH2_PROXY_CLIENT_SECRET": "$(.client-secret.data.password)",
                                },
                            },
                        },
                    }
                ]
            )

            kubectl.wait_for_condition(
                api_version="secretgen.carvel.dev/v1alpha1",
                kind="SecretTemplate",
                name=name,
                namespace=ns,
                condition_type="ReconcileSucceeded",
                condition_status="True",
                timeout_seconds=120,
            )

    # ----------------------------
    # Keycloak side operations
    # ----------------------------
    def ensure_realm(self) -> None:
        if not self.kc.realm_exists(self.cfg.realm.realm):
            self.kc.create_realm(
                realm=self.cfg.realm.realm,
                display_name=self.cfg.realm.display_name,
                enabled=self.cfg.realm.enabled,
            )

    def ensure_clientscope_roles_mapper(self) -> None:
        """
        Adds the protocol mapper under "roles" scope per-client.
        In your Ansible role, they reconfigure the mapper per client_id.
        That means: do it once per client, because claim.name includes client_id.
        """
        for c in self.cfg.clients:
            self.kc.ensure_roles_mapper_clientscope(realm=self.cfg.realm.realm, client_id=c.id)

    def ensure_clients_and_roles(self, kubectl) -> None:
        """
        Uses secrets generated in Kubernetes as Keycloak client secrets.
        """
        for c in self.cfg.clients:
            secret = self.read_client_secret(kubectl, client_id=c.id)
            self.kc.create_or_update_client(
                realm=self.cfg.realm.realm,
                client_id=c.id,
                secret=secret,
                redirect_uris=c.redirect_uris,
            )
            if c.roles:
                self.kc.ensure_client_roles(realm=self.cfg.realm.realm, client_id=c.id, roles=c.roles)

    # ----------------------------
    # Orchestrator
    # ----------------------------
    def run(self, kubectl) -> None:
        """
        Full parity sequence:
        1) Generate Passwords for client secrets
        2) Ensure realm
        3) Ensure roles mapper per client
        4) Create clients using generated secrets
        5) Create roles
        6) Generate cookie secrets + oauth2-proxy SecretTemplates
        """
        self.ensure_client_secret_passwords(kubectl)

        self.ensure_realm()
        self.ensure_clientscope_roles_mapper()

        self.ensure_clients_and_roles(kubectl)

        self.ensure_cookie_secrets_for_oauth2(kubectl)
        self.ensure_oauth2_proxy_secret_templates(kubectl)
