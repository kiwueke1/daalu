# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/iam/keycloak.py

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

import requests

from daalu.bootstrap.shared.keycloak.models import (
    KeycloakIAMConfig,
    KeycloakClientSpec,
)


class KeycloakIAMError(RuntimeError):
    pass


class KeycloakIAMManager:
    """
    Minimal, idempotent Keycloak bootstrapper:
      - login (admin token)
      - ensure realm exists
      - ensure client exists
      - ensure roles exist
      - optionally fetch client secret (for confidential clients)
    """

    def __init__(self, *, config: KeycloakIAMConfig):
        self.config = config
        self._token: Optional[str] = None

    # -----------------------
    # HTTP helpers
    # -----------------------
    def _base_admin_url(self) -> str:
        base = str(self.config.admin.base_url).rstrip("/")
        return f"{base}/admin/realms"

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise KeycloakIAMError("Not authenticated")
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def login(self) -> None:
        """
        Get admin access token using the OpenID token endpoint.
        """
        base = str(self.config.admin.base_url).rstrip("/")
        realm = self.config.admin.admin_realm
        token_url = f"{base}/realms/{realm}/protocol/openid-connect/token"

        data = {
            "grant_type": "password",
            "client_id": self.config.admin.admin_client_id,
            "username": self.config.admin.username,
            "password": self.config.admin.password,
        }

        r = requests.post(token_url, data=data, verify=self.config.admin.verify_tls, timeout=30)
        if r.status_code != 200:
            raise KeycloakIAMError(f"Keycloak login failed: {r.status_code} {r.text}")

        self._token = r.json()["access_token"]

    # -----------------------
    # Realm
    # -----------------------
    def ensure_realm(self) -> None:
        realm = self.config.realm.realm
        url = f"{self._base_admin_url()}/{realm}"

        r = requests.get(url, headers=self._headers(), verify=self.config.admin.verify_tls, timeout=30)
        if r.status_code == 200:
            return
        if r.status_code != 404:
            raise KeycloakIAMError(f"Failed to query realm {realm}: {r.status_code} {r.text}")

        create_url = self._base_admin_url()
        payload = {
            "realm": realm,
            "enabled": self.config.realm.enabled,
            "displayName": self.config.realm.display_name,
        }

        r = requests.post(create_url, json=payload, headers=self._headers(), verify=self.config.admin.verify_tls, timeout=30)
        if r.status_code not in (201, 204):
            raise KeycloakIAMError(f"Failed to create realm {realm}: {r.status_code} {r.text}")

    # -----------------------
    # Clients
    # -----------------------
    def _find_client_uuid(self, *, realm: str, client_id: str) -> Optional[str]:
        url = f"{self._base_admin_url()}/{realm}/clients"
        r = requests.get(url, params={"clientId": client_id}, headers=self._headers(), verify=self.config.admin.verify_tls, timeout=30)
        if r.status_code != 200:
            raise KeycloakIAMError(f"Failed to query clients: {r.status_code} {r.text}")

        items = r.json()
        if not items:
            return None
        return items[0]["id"]

    def ensure_client(self, client: KeycloakClientSpec) -> str:
        """
        Ensure a client exists; returns the Keycloak client UUID.
        """
        realm = self.config.realm.realm
        client_uuid = self._find_client_uuid(realm=realm, client_id=client.id)
        if client_uuid:
            # Optional: could PATCH updates here later
            return client_uuid

        url = f"{self._base_admin_url()}/{realm}/clients"

        payload = {
            "clientId": client.id,
            "enabled": True,
            "protocol": client.protocol,
            "publicClient": client.public,
            "redirectUris": client.redirect_uris or [],
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": True,
        }

        if client.root_url:
            payload["rootUrl"] = client.root_url
        if client.base_url:
            payload["baseUrl"] = client.base_url

        r = requests.post(url, json=payload, headers=self._headers(), verify=self.config.admin.verify_tls, timeout=30)
        if r.status_code not in (201, 204):
            raise KeycloakIAMError(f"Failed to create client {client.id}: {r.status_code} {r.text}")

        # now it exists
        client_uuid = self._find_client_uuid(realm=realm, client_id=client.id)
        if not client_uuid:
            raise KeycloakIAMError(f"Created client {client.id} but failed to find it afterwards")
        return client_uuid

    def ensure_client_roles(self, *, client_uuid: str, roles: list[str]) -> None:
        if not roles:
            return

        realm = self.config.realm.realm
        for role in roles:
            url = f"{self._base_admin_url()}/{realm}/clients/{client_uuid}/roles/{role}"
            r = requests.get(url, headers=self._headers(), verify=self.config.admin.verify_tls, timeout=30)
            if r.status_code == 200:
                continue
            if r.status_code != 404:
                raise KeycloakIAMError(f"Failed to query role {role}: {r.status_code} {r.text}")

            create_url = f"{self._base_admin_url()}/{realm}/clients/{client_uuid}/roles"
            payload = {"name": role}
            r = requests.post(create_url, json=payload, headers=self._headers(), verify=self.config.admin.verify_tls, timeout=30)
            if r.status_code not in (201, 204):
                raise KeycloakIAMError(f"Failed to create role {role}: {r.status_code} {r.text}")

    def get_client_secret(self, *, client_uuid: str) -> str:
        """
        Only valid for confidential clients.
        """
        realm = self.config.realm.realm
        url = f"{self._base_admin_url()}/{realm}/clients/{client_uuid}/client-secret"
        r = requests.get(url, headers=self._headers(), verify=self.config.admin.verify_tls, timeout=30)
        if r.status_code != 200:
            raise KeycloakIAMError(f"Failed to get client secret: {r.status_code} {r.text}")
        return r.json()["value"]

    def ensure_required_action(
        self,
        *,
        realm: str,
        alias: str,
        name: str,
        enabled: bool = True,
        default_action: bool = True,
    ):
        """
        Ensure a Keycloak Required Action exists and is configured.

        Mirrors:
        community.general.keycloak_authentication_required_actions
        """

        self._ensure_logged_in()

        url = (
            f"{self.admin.base_url.rstrip('/')}"
            f"/admin/realms/{realm}/authentication/required-actions/{alias}"
        )

        payload = {
            "alias": alias,
            "name": name,
            "providerId": alias,
            "enabled": enabled,
            "defaultAction": default_action,
        }

        r = self.session.put(url, json=payload, verify=self.admin.verify_tls)

        # Keycloak returns:
        # - 204 if updated
        # - 201 if created
        # - 404 if missing (older KC versions)
        if r.status_code in (200, 201, 204):
            return

        if r.status_code == 404:
            # Create if missing
            create_url = (
                f"{self.admin.base_url.rstrip('/')}"
                f"/admin/realms/{realm}/authentication/required-actions"
            )

            r = self.session.post(create_url, json=payload, verify=self.admin.verify_tls)
            r.raise_for_status()
            return

        r.raise_for_status()
