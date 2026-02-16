# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/shared/keycloak/iam.py

from __future__ import annotations

import base64
import secrets
import string
from dataclasses import dataclass
from typing import List

from daalu.bootstrap.shared.keycloak.admin import KeycloakAdmin
from daalu.bootstrap.shared.keycloak.models import (
    KeycloakAdminAuth,
    KeycloakClientSpec,
    KeycloakRealmSpec,
    KeycloakIAMConfig,
)


def _generate_password(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class KeycloakIAMManager:
    """
    Shared helper to get Ansible parity:
    - secretgen Password CRs -> Secret values
    - Keycloak realm + clients + roles
    - secretgen SecretTemplate for oauth2-proxy env vars

    When secretgen-controller is installed, uses Password CRDs.
    Otherwise falls back to generating secrets in Python.
    """

    def __init__(self, cfg: KeycloakIAMConfig):
        self.cfg = cfg
        self.kc = KeycloakAdmin(cfg.admin)
        self._secretgen_available: bool | None = None

    def _has_secretgen(self, kubectl) -> bool:
        if self._secretgen_available is None:
            rc, _, _ = kubectl._run("api-resources --api-group=secretgen.k14s.io")
            self._secretgen_available = rc == 0 and True
            # Double-check: rc==0 but empty output means the group doesn't exist
            if rc == 0:
                rc2, out, _ = kubectl._run(
                    "api-resources --api-group=secretgen.k14s.io -o name"
                )
                self._secretgen_available = bool(out.strip())
        return self._secretgen_available

    # ----------------------------
    # Kubernetes secretgen helpers
    # ----------------------------
    def _ensure_password_secret(self, kubectl, *, name: str, ns: str, length: int) -> None:
        """Create a Secret with a random password, using secretgen if available."""
        if self._has_secretgen(kubectl):
            kubectl.apply_objects(
                [
                    {
                        "apiVersion": "secretgen.k14s.io/v1alpha1",
                        "kind": "Password",
                        "metadata": {"name": name, "namespace": ns},
                        "spec": {"length": length},
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
        else:
            # Check if the secret already exists to avoid overwriting
            existing = kubectl.get_object(
                api_version="v1", kind="Secret", name=name, namespace=ns,
            )
            if existing:
                return
            pw = _generate_password(length)
            kubectl.apply_objects(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": name, "namespace": ns},
                        "stringData": {"password": pw},
                    }
                ]
            )

    def ensure_client_secret_passwords(self, kubectl) -> None:
        """
        Equivalent to:
        - kind: Password (secretgen.k14s.io)
        - wait for ReconcileSucceeded

        Falls back to plain Secrets with Python-generated passwords
        when secretgen-controller is not installed.
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
            self._ensure_password_secret(kubectl, name=name, ns=ns, length=64)

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
            self._ensure_password_secret(kubectl, name=name, ns=ns, length=32)

    def _build_oauth2_proxy_string_data(self, c: KeycloakClientSpec) -> dict:
        return {
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
            "OAUTH2_PROXY_ALLOWED_ROLE": f"{c.id}:{c.roles[0]}",
            "OAUTH2_PROXY_CODE_CHALLENGE_METHOD": "S256",
        }

    def ensure_oauth2_proxy_secret_templates(self, kubectl) -> None:
        """
        Equivalent to your SecretTemplate in Ansible.
        Produces Secret with env vars for oauth2-proxy sidecar.

        When secretgen-controller is available, uses SecretTemplate CRDs
        to dynamically compose secrets. Otherwise reads the password secrets
        directly and builds the oauth2-proxy Secret in Python.
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

            if self._has_secretgen(kubectl):
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
                                    "stringData": self._build_oauth2_proxy_string_data(c),
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
            else:
                # Read the password secrets directly and compose the Secret
                client_secret = self.read_client_secret(kubectl, client_id=c.id)
                cookie_secret_obj = kubectl.get_object(
                    api_version="v1", kind="Secret",
                    name=f"{c.id}-cookie-secret", namespace=ns,
                )
                cookie_pw = base64.b64decode(
                    cookie_secret_obj["data"]["password"]
                ).decode("utf-8", errors="replace")

                string_data = self._build_oauth2_proxy_string_data(c)
                string_data["OAUTH2_PROXY_COOKIE_SECRET"] = cookie_pw
                string_data["OAUTH2_PROXY_CLIENT_SECRET"] = client_secret

                kubectl.apply_objects(
                    [
                        {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "metadata": {"name": name, "namespace": ns},
                            "stringData": string_data,
                        }
                    ]
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
