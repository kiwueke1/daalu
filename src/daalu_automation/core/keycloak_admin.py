"""Idempotent Keycloak Admin REST helper.

Used to keep the Keycloak realm in sync with daalu's own user table so a
fresh install (and every new invite) comes up with a working SSO login:

- mirror a daalu ``User`` into the realm (``ensure_user`` + ``set_password``),
- clear the new-user required-actions / verify-profile gate that otherwise
  blocks direct logins,
- self-heal the hub OIDC client (``ensure_client``) if it's missing.

It authenticates with a confidential client's service account
(``keycloak_admin_client_id`` / ``_secret``) that holds ``manage-users``
(+ ``manage-clients`` for client self-heal). Realm + base URL are derived
from ``keycloak_issuer_url`` when not set explicitly. This is the runtime
counterpart to the *declarative* client/realm provisioning in the platform
repo (``cluster-defs`` → realm ``daalu``); both converge on the same shapes.

Kept dependency-light (httpx + settings), mirroring ``core/keycloak.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import structlog

from daalu_automation.config import get_settings

logger = structlog.get_logger(__name__)

_EXPIRY_SKEW_S = 30.0


class KeycloakAdminError(RuntimeError):
    """Raised when an admin operation cannot be completed."""


def _derive_base_and_realm() -> tuple[str, str]:
    """Return (base_url, realm) from explicit settings or the issuer URL.

    ``keycloak_issuer_url`` is ``https://<host>/realms/<realm>``; the admin
    REST base is ``https://<host>`` and the realm is the trailing segment.
    """
    settings = get_settings()
    base = settings.keycloak_admin_base_url.rstrip("/")
    realm = settings.keycloak_realm
    if not base or not realm:
        issuer = (settings.keycloak_issuer_url or "").rstrip("/")
        if "/realms/" not in issuer:
            raise KeycloakAdminError(
                "cannot derive Keycloak admin base/realm: set keycloak_admin_base_url "
                "+ keycloak_realm, or a keycloak_issuer_url of the form "
                "https://<host>/realms/<realm>"
            )
        host, _, derived_realm = issuer.partition("/realms/")
        base = base or host
        realm = realm or derived_realm.split("/")[0]
    return base, realm


@dataclass
class _Token:
    access_token: str
    expires_at: float  # monotonic seconds


class KeycloakAdmin:
    """Thin async wrapper over the Keycloak Admin REST API (one realm)."""

    def __init__(self) -> None:
        settings = get_settings()
        # Two supported auth modes (client-credentials preferred); the realm
        # the admin authenticates against may differ from the managed realm.
        self._cc = bool(
            settings.keycloak_admin_client_id and settings.keycloak_admin_client_secret
        )
        self._pw = bool(
            settings.keycloak_admin_username and settings.keycloak_admin_password
        )
        if not self._cc and not self._pw:
            raise KeycloakAdminError(
                "no Keycloak admin credentials configured: set "
                "keycloak_admin_client_id + _secret (client-credentials) or "
                "keycloak_admin_username + _password (password grant)"
            )
        self._client_id = settings.keycloak_admin_client_id
        self._client_secret = settings.keycloak_admin_client_secret
        self._username = settings.keycloak_admin_username
        self._password = settings.keycloak_admin_password
        self._admin_realm = settings.keycloak_admin_realm or "master"
        self._cli_client_id = settings.keycloak_admin_cli_client_id or "admin-cli"
        self._base, self._realm = _derive_base_and_realm()
        self._token: _Token | None = None

    @property
    def realm(self) -> str:
        return self._realm

    async def _bearer(self, client: httpx.AsyncClient) -> str:
        now = time.monotonic()
        if self._token and self._token.expires_at - _EXPIRY_SKEW_S > now:
            return self._token.access_token
        if self._cc:
            # Service account in the managed realm.
            token_url = f"{self._base}/realms/{self._realm}/protocol/openid-connect/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        else:
            # Realm admin user (often master) via admin-cli.
            token_url = (
                f"{self._base}/realms/{self._admin_realm}/protocol/openid-connect/token"
            )
            data = {
                "grant_type": "password",
                "client_id": self._cli_client_id,
                "username": self._username,
                "password": self._password,
            }
        resp = await client.post(token_url, data=data)
        if resp.status_code != 200:
            raise KeycloakAdminError(
                f"admin token request returned {resp.status_code}: {resp.text[:200]}"
            )
        body = resp.json()
        self._token = _Token(
            access_token=body["access_token"],
            expires_at=now + float(body.get("expires_in", 60)),
        )
        return self._token.access_token

    def _admin(self, path: str) -> str:
        return f"{self._base}/admin/realms/{self._realm}{path}"

    async def ensure_user(
        self,
        *,
        email: str,
        first_name: str | None = None,
        last_name: str | None = None,
        enabled: bool = True,
        email_verified: bool = True,
    ) -> str:
        """Create-or-update a realm user keyed by email; return its id.

        Idempotent: re-running updates the profile fields and clears any
        pending required actions (so direct logins aren't blocked by the
        realm's Verify-Profile / Update-Password gate). Username == email.
        """
        email = email.lower()
        # Reasonable display-name fallback so Verify-Profile is satisfied.
        first = first_name or (email.split("@")[0] or "Daalu")
        last = last_name or "User"
        async with httpx.AsyncClient(timeout=20.0) as client:
            token = await self._bearer(client)
            headers = {"Authorization": f"Bearer {token}"}
            # Look up by exact email.
            q = await client.get(
                self._admin("/users"),
                params={"email": email, "exact": "true"},
                headers=headers,
            )
            if q.status_code != 200:
                raise KeycloakAdminError(
                    f"user lookup for {email} returned {q.status_code}: {q.text[:200]}"
                )
            found = q.json()
            payload = {
                "username": email,
                "email": email,
                "firstName": first,
                "lastName": last,
                "enabled": enabled,
                "emailVerified": email_verified,
                "requiredActions": [],
            }
            if found:
                uid = found[0]["id"]
                u = await client.put(self._admin(f"/users/{uid}"), json=payload, headers=headers)
                if u.status_code not in (204, 200):
                    raise KeycloakAdminError(
                        f"user update for {email} returned {u.status_code}: {u.text[:200]}"
                    )
                logger.debug("keycloak_admin.user_updated", email=email, id=uid)
                return uid
            c = await client.post(self._admin("/users"), json=payload, headers=headers)
            if c.status_code not in (201, 204):
                raise KeycloakAdminError(
                    f"user create for {email} returned {c.status_code}: {c.text[:200]}"
                )
            # Keycloak returns the new id in the Location header.
            loc = c.headers.get("Location", "")
            uid = loc.rstrip("/").split("/")[-1]
            logger.info("keycloak_admin.user_created", email=email, id=uid)
            return uid

    async def set_password(self, *, user_id: str, password: str, temporary: bool = True) -> None:
        """Set a user's password (``temporary`` forces a change at next login)."""
        async with httpx.AsyncClient(timeout=20.0) as client:
            token = await self._bearer(client)
            r = await client.put(
                self._admin(f"/users/{user_id}/reset-password"),
                json={"type": "password", "value": password, "temporary": temporary},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code not in (204, 200):
                raise KeycloakAdminError(
                    f"set-password returned {r.status_code}: {r.text[:200]}"
                )

    async def ensure_client(
        self,
        *,
        client_id: str,
        redirect_uris: list[str],
        client_secret: str | None = None,
        public: bool = False,
    ) -> None:
        """Create-or-update a confidential OIDC client (self-heal path).

        The authoritative definition is the platform repo's declarative
        provisioning; this only guarantees the client exists at runtime so
        the hub login flow can't break if the realm was rebuilt without it.
        """
        async with httpx.AsyncClient(timeout=20.0) as client:
            token = await self._bearer(client)
            headers = {"Authorization": f"Bearer {token}"}
            q = await client.get(
                self._admin("/clients"), params={"clientId": client_id}, headers=headers
            )
            if q.status_code != 200:
                raise KeycloakAdminError(
                    f"client lookup for {client_id} returned {q.status_code}"
                )
            payload = {
                "clientId": client_id,
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": public,
                "standardFlowEnabled": True,
                "redirectUris": redirect_uris,
            }
            if client_secret and not public:
                payload["secret"] = client_secret
            existing = q.json()
            if existing:
                cid = existing[0]["id"]
                r = await client.put(self._admin(f"/clients/{cid}"), json=payload, headers=headers)
                if r.status_code not in (204, 200):
                    raise KeycloakAdminError(
                        f"client update for {client_id} returned {r.status_code}: {r.text[:200]}"
                    )
            else:
                r = await client.post(self._admin("/clients"), json=payload, headers=headers)
                if r.status_code not in (201, 204):
                    raise KeycloakAdminError(
                        f"client create for {client_id} returned {r.status_code}: {r.text[:200]}"
                    )
            logger.info("keycloak_admin.client_ensured", client_id=client_id)

    async def ensure_audience_mapper(
        self,
        *,
        client_id: str,
        audience_client: str,
        mapper_name: str = "nvcm-exchange-audience",
    ) -> None:
        """Ensure an OIDC audience protocol-mapper on ``client_id`` that stamps
        ``audience_client`` into issued tokens' ``aud``.

        Required so Keycloak permits ``audience_client`` (daalu-hub-nvcm) to run
        a Standard Token Exchange on this client's user tokens — the per-user
        NV-CM tool SSO path (see api/tool_proxy.get_user_nvcm_token). Idempotent.
        """
        async with httpx.AsyncClient(timeout=20.0) as client:
            token = await self._bearer(client)
            headers = {"Authorization": f"Bearer {token}"}
            q = await client.get(
                self._admin("/clients"), params={"clientId": client_id}, headers=headers
            )
            existing = q.json() if q.status_code == 200 else []
            if not existing:
                raise KeycloakAdminError(
                    f"client {client_id} not found for audience mapper"
                )
            cid = existing[0]["id"]
            m = await client.get(
                self._admin(f"/clients/{cid}/protocol-mappers/models"), headers=headers
            )
            if m.status_code == 200 and any(
                pm.get("name") == mapper_name for pm in m.json()
            ):
                return  # already present
            body = {
                "name": mapper_name,
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.client.audience": audience_client,
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            }
            r = await client.post(
                self._admin(f"/clients/{cid}/protocol-mappers/models"),
                json=body,
                headers=headers,
            )
            if r.status_code not in (201, 204, 409):
                raise KeycloakAdminError(
                    f"audience mapper on {client_id} returned {r.status_code}: {r.text[:160]}"
                )
        logger.info(
            "keycloak_admin.audience_mapper_ensured",
            client_id=client_id,
            audience=audience_client,
        )

    async def ensure_sso_session_max_lifespan(self, *, seconds: int) -> bool:
        """Raise the realm's ``ssoSessionMaxLifespan`` to at least ``seconds``.

        Keycloak clamps a **token-exchanged** access token's ``exp`` to the end
        of the subject's SSO session (``auth_time + ssoSessionMaxLifespan``) —
        and it does so *silently*: past that point the exchange still returns
        HTTP 200, with a negative ``expires_in`` and a token that is already
        expired when minted. The NV-CM gateway then answers every tool request
        with ``401 Jwt is expired``.

        The offline refresh token the hub stores (``krt``) does NOT lift this:
        ``offlineSessionMaxLifespanEnabled`` changes nothing here, only the SSO
        max does. So whenever the hub session cookie outlives the realm's SSO
        session (the default is 10h against a 24h cookie), there is a window
        where the user is still logged into the hub — nothing prompts a fresh
        login — but per-user tool SSO is dead. Keeping the realm's ceiling above
        the cookie's lifetime closes that window.

        Never lowers an operator's higher value. Returns True if it changed.
        """
        async with httpx.AsyncClient(timeout=20.0) as client:
            token = await self._bearer(client)
            headers = {"Authorization": f"Bearer {token}"}
            q = await client.get(self._admin(""), headers=headers)
            if q.status_code != 200:
                raise KeycloakAdminError(
                    f"realm read returned {q.status_code}: {q.text[:160]}"
                )
            realm = q.json()
            current = int(realm.get("ssoSessionMaxLifespan") or 0)
            if current >= seconds:
                return False
            # Keycloak's realm endpoint takes a full representation; mutate the
            # one we just read so no unrelated setting is dropped.
            realm["ssoSessionMaxLifespan"] = seconds
            r = await client.put(self._admin(""), json=realm, headers=headers)
            if r.status_code not in (200, 204):
                raise KeycloakAdminError(
                    f"realm update returned {r.status_code}: {r.text[:160]}"
                )
        logger.info(
            "keycloak_admin.sso_session_max_lifespan_raised",
            was=current,
            now=seconds,
        )
        return True

    async def ensure_realm_role(self, *, name: str, description: str = "") -> None:
        """Create the realm role ``name`` if it doesn't exist. Idempotent.

        NV-CM's Nautobot maps JWT ``roles`` claims against
        ``nautobot.rbac.superuserGroups`` — a role name that exists nowhere in
        the realm can never match, which reads as a blanket "Access Denied"
        for every SSO'd user (see keycloak_nvcm_superuser_roles).
        """
        async with httpx.AsyncClient(timeout=20.0) as client:
            token = await self._bearer(client)
            headers = {"Authorization": f"Bearer {token}"}
            q = await client.get(self._admin(f"/roles/{name}"), headers=headers)
            if q.status_code == 200:
                return
            if q.status_code != 404:
                raise KeycloakAdminError(
                    f"role lookup for {name} returned {q.status_code}"
                )
            r = await client.post(
                self._admin("/roles"),
                json={"name": name, "description": description},
                headers=headers,
            )
            if r.status_code not in (201, 204, 409):
                raise KeycloakAdminError(
                    f"role create for {name} returned {r.status_code}: {r.text[:160]}"
                )
        logger.info("keycloak_admin.realm_role_ensured", role=name)

    async def grant_realm_role_to_service_account(
        self, *, client_id: str, role_name: str
    ) -> bool:
        """Grant realm role ``role_name`` to ``client_id``'s service account.

        Returns False (without raising) when the client doesn't exist yet —
        on a fresh install the NV-CM client may be provisioned later; the
        next Job run converges. Idempotent otherwise.
        """
        async with httpx.AsyncClient(timeout=20.0) as client:
            token = await self._bearer(client)
            headers = {"Authorization": f"Bearer {token}"}
            q = await client.get(
                self._admin("/clients"), params={"clientId": client_id}, headers=headers
            )
            existing = q.json() if q.status_code == 200 else []
            if not existing:
                logger.warning(
                    "keycloak_admin.service_account_role_skipped",
                    client_id=client_id,
                    role=role_name,
                    reason="client not found",
                )
                return False
            cid = existing[0]["id"]
            sa = await client.get(
                self._admin(f"/clients/{cid}/service-account-user"), headers=headers
            )
            if sa.status_code != 200:
                raise KeycloakAdminError(
                    f"service-account lookup for {client_id} returned {sa.status_code}"
                )
            uid = sa.json()["id"]
            role = await client.get(self._admin(f"/roles/{role_name}"), headers=headers)
            if role.status_code != 200:
                raise KeycloakAdminError(
                    f"role lookup for {role_name} returned {role.status_code}"
                )
            r = await client.post(
                self._admin(f"/users/{uid}/role-mappings/realm"),
                json=[role.json()],
                headers=headers,
            )
            if r.status_code not in (204, 200, 409):
                raise KeycloakAdminError(
                    f"role grant {role_name} → {client_id} service account "
                    f"returned {r.status_code}: {r.text[:160]}"
                )
        logger.info(
            "keycloak_admin.service_account_role_granted",
            client_id=client_id,
            role=role_name,
        )
        return True

    async def ensure_identity_provider_google(
        self, *, client_id: str, client_secret: str, alias: str = "google"
    ) -> None:
        """Create-or-update a Google identity provider on the managed realm.

        Adds the "Sign in with Google" button to the realm login page and
        brokers logins to Google. ``trustEmail`` is on (Google verifies the
        address) so brokered users land with a verified email — which is the
        key the hub maps onto the daalu ``User`` row. Idempotent.
        """
        config = {
            "clientId": client_id,
            "clientSecret": client_secret,
            "useJwksUrl": "true",
            "syncMode": "IMPORT",
        }
        payload = {
            "alias": alias,
            "providerId": "google",
            "enabled": True,
            "trustEmail": True,
            "storeToken": False,
            "config": config,
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            token = await self._bearer(client)
            headers = {"Authorization": f"Bearer {token}"}
            existing = await client.get(
                self._admin(f"/identity-provider/instances/{alias}"), headers=headers
            )
            if existing.status_code == 200:
                r = await client.put(
                    self._admin(f"/identity-provider/instances/{alias}"),
                    json=payload,
                    headers=headers,
                )
                if r.status_code not in (204, 200):
                    raise KeycloakAdminError(
                        f"IdP update for {alias} returned {r.status_code}: {r.text[:200]}"
                    )
            else:
                r = await client.post(
                    self._admin("/identity-provider/instances"),
                    json=payload,
                    headers=headers,
                )
                if r.status_code not in (201, 204):
                    raise KeycloakAdminError(
                        f"IdP create for {alias} returned {r.status_code}: {r.text[:200]}"
                    )
            logger.info("keycloak_admin.idp_ensured", alias=alias)
