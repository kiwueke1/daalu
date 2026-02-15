# src/daalu/bootstrap/shared/keycloak/admin.py

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional
import urllib.parse
import urllib.request

from daalu.bootstrap.shared.keycloak.models import KeycloakAdminAuth
import logging

log = logging.getLogger("daalu")


class KeycloakError(RuntimeError):
    pass


@dataclass
class _HttpResponse:
    status: int
    body: str


def _http_request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    verify_tls: bool = True,
) -> _HttpResponse:
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)

    # NOTE: If you need custom TLS behavior, we can add ssl.SSLContext here.
    # For now, urllib honors system certs. For "verify_tls=False" we *should*
    # build an unverified context; implement only if you truly need it.
    # Given your environment, it may be fine to keep verify_tls=True and
    # trust your cert-manager issuer.
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return _HttpResponse(status=resp.status, body=body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return _HttpResponse(status=e.code, body=body)


class KeycloakAdmin:
    """
    Tiny Keycloak Admin API wrapper (no third-party deps).

    Endpoints used:
    - token: /realms/<admin_realm>/protocol/openid-connect/token
    - admin: /admin/realms/...
    """

    def __init__(self, auth: KeycloakAdminAuth):
        self.auth = auth
        self._token: Optional[str] = None

    def _token_url(self) -> str:
        base = str(self.auth.base_url).rstrip("/")
        return f"{base}/realms/{self.auth.admin_realm}/protocol/openid-connect/token"


    def _admin_url(self, path: str) -> str:
        base = str(self.auth.base_url).rstrip("/")
        path = path.lstrip("/")
        return f"{base}/{path}"

    def login(self) -> None:
        payload = {
            "grant_type": "password",
            "client_id": self.auth.admin_client_id,
            "username": self.auth.username,
            "password": self.auth.password,
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")

        r = _http_request(
            "POST",
            self._token_url(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=data,
            verify_tls=self.auth.verify_tls,
        )
        if r.status < 200 or r.status >= 300:
            raise KeycloakError(f"Keycloak login failed ({r.status}): {r.body}")

        obj = json.loads(r.body)
        token = obj.get("access_token")
        if not token:
            raise KeycloakError(f"Keycloak login missing access_token: {r.body}")
        self._token = token

    def _headers(self) -> Dict[str, str]:
        if not self._token:
            self.login()
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    # ----------------------------
    # Realm operations
    # ----------------------------
    def realm_exists_1(self, realm: str) -> bool:
        r = _http_request(
            "GET",
            self._admin_url(f"/admin/realms/{realm}"),
            headers=self._headers(),
            verify_tls=self.auth.verify_tls,
        )
        return r.status == 200

    def realm_exists(self, realm: str) -> bool:
        url = self._admin_url(f"/admin/realms/{realm}")
        headers = self._headers()

        # 🔎 DEBUG
        log.debug("[KeycloakAdmin] realm_exists()")
        log.debug(f"  base_url   = {self.auth.base_url}")
        log.debug(f"  admin_url  = {url}")
        log.debug(f"  realm      = {realm}")
        log.debug(f"  verify_tls = {self.auth.verify_tls}")
        log.debug(f"  headers    = {{'Authorization': 'Bearer <redacted>'}}")
        log.debug("-" * 60)

        r = _http_request(
            "GET",
            url,
            headers=headers,
            verify_tls=self.auth.verify_tls,
        )

        log.debug(f"[KeycloakAdmin] response: status={r.status}, body={r.body}")
        log.debug("-" * 60)

        return r.status == 200

    def create_realm(self, *, realm: str, display_name: str, enabled: bool = True) -> None:
        payload = {
            "realm": realm,
            "id": realm,
            "displayName": display_name,
            "enabled": enabled,
        }
        r = _http_request(
            "POST",
            self._admin_url("/admin/realms"),
            headers=self._headers(),
            data=json.dumps(payload).encode("utf-8"),
            verify_tls=self.auth.verify_tls,
        )
        if r.status not in (201, 204):
            # Keycloak sometimes returns 409 if it already exists
            if r.status == 409:
                return
            raise KeycloakError(f"Create realm failed ({r.status}): {r.body}")

    # ----------------------------
    # Client scope "roles" mapper
    # ----------------------------
    def ensure_roles_mapper_clientscope(self, *, realm: str, client_id: str) -> None:
        """
        Parity with your Ansible:
        client scope: roles
        protocol mapper: oidc-usermodel-client-role-mapper
        claim: resource_access.<client_id>.roles
        """
        # 1) Find client scope "roles"
        scopes = self._get(f"/admin/realms/{realm}/client-scopes")
        roles_scope = next((s for s in scopes if s.get("name") == "roles"), None)
        if not roles_scope:
            raise KeycloakError(f'Expected client-scope "roles" to exist in realm {realm}')

        scope_id = roles_scope["id"]

        # 2) Get protocol mappers for that scope
        mappers = self._get(f"/admin/realms/{realm}/client-scopes/{scope_id}/protocol-mappers/models")
        wanted_name = "client roles"
        exists = any(m.get("name") == wanted_name for m in mappers)
        if exists:
            return

        payload = {
            "name": wanted_name,
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-client-role-mapper",
            "config": {
                "claim.name": f"resource_access.{client_id}.roles",
                "access.token.claim": "true",
                "id.token.claim": "true",
                "multivalued": "true",
            },
        }

        r = _http_request(
            "POST",
            self._admin_url(f"/admin/realms/{realm}/client-scopes/{scope_id}/protocol-mappers/models"),
            headers=self._headers(),
            data=json.dumps(payload).encode("utf-8"),
            verify_tls=self.auth.verify_tls,
        )
        if r.status not in (201, 204):
            raise KeycloakError(f"Create clientscope mapper failed ({r.status}): {r.body}")

    # ----------------------------
    # Clients + roles
    # ----------------------------
    def get_client_uuid(self, *, realm: str, client_id: str) -> Optional[str]:
        items = self._get(f"/admin/realms/{realm}/clients?clientId={urllib.parse.quote(client_id)}")
        if not items:
            return None
        return items[0].get("id")

    def create_or_update_client(
        self,
        *,
        realm: str,
        client_id: str,
        secret: str,
        redirect_uris: list[str],
    ) -> None:
        """
        Equivalent to community.general.keycloak_client.
        """
        existing_uuid = self.get_client_uuid(realm=realm, client_id=client_id)
        payload = {
            "clientId": client_id,
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "redirectUris": redirect_uris,
            "secret": secret,
        }

        if existing_uuid:
            r = _http_request(
                "PUT",
                self._admin_url(f"/admin/realms/{realm}/clients/{existing_uuid}"),
                headers=self._headers(),
                data=json.dumps(payload).encode("utf-8"),
                verify_tls=self.auth.verify_tls,
            )
            if r.status not in (200, 204):
                raise KeycloakError(f"Update client failed ({r.status}): {r.body}")
        else:
            r = _http_request(
                "POST",
                self._admin_url(f"/admin/realms/{realm}/clients"),
                headers=self._headers(),
                data=json.dumps(payload).encode("utf-8"),
                verify_tls=self.auth.verify_tls,
            )
            if r.status not in (201, 204):
                # 409 if already exists
                if r.status == 409:
                    return
                raise KeycloakError(f"Create client failed ({r.status}): {r.body}")

    def ensure_client_roles(self, *, realm: str, client_id: str, roles: list[str]) -> None:
        client_uuid = self.get_client_uuid(realm=realm, client_id=client_id)
        if not client_uuid:
            raise KeycloakError(f"Client {client_id} not found in realm {realm}")

        existing = self._get(f"/admin/realms/{realm}/clients/{client_uuid}/roles")
        existing_names = {r.get("name") for r in existing}

        for role in roles:
            if role in existing_names:
                continue
            payload = {"name": role}
            r = _http_request(
                "POST",
                self._admin_url(f"/admin/realms/{realm}/clients/{client_uuid}/roles"),
                headers=self._headers(),
                data=json.dumps(payload).encode("utf-8"),
                verify_tls=self.auth.verify_tls,
            )
            if r.status not in (201, 204):
                if r.status == 409:
                    continue
                raise KeycloakError(f"Create role failed ({r.status}): {r.body}")

    # ----------------------------
    # Internal JSON helpers
    # ----------------------------
    def _get(self, path: str) -> Any:
        r = _http_request(
            "GET",
            self._admin_url(path),
            headers=self._headers(),
            verify_tls=self.auth.verify_tls,
        )
        if r.status != 200:
            raise KeycloakError(f"GET {path} failed ({r.status}): {r.body}")
        return json.loads(r.body or "null")
