"""HTTP clients for NV-CM Config Store / Render / Temporal-workflow APIs.

Each client wraps the tenant's ``svc-*`` base URL and injects a Keycloak
client-credentials JWT (minted + cached by :mod:`core.keycloak`). The
connection parameters come from the tenant's
``Integration(provider="config_manager")`` row (see ``conn_from_integration_config``).

Endpoints are pinned to NV-CM ``v1`` (OpenAPI specs vendored under
``deploy/charts/nv-config-manager-<ver>-api-specs/``):

* Config Store — ``POST/GET /v1/config/{device_uuid}/{filename}``,
  ``.../versions``, ``.../diff``.
* Render — ``POST /v1/render/{device_uuid}/render``.
* Temporal — ``POST /v1/workflow/ngc/deploy``, ``GET /v1/workflow/{id}``,
  ``POST /v1/workflow/{id}/approve/{stage}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from daalu_automation.core.crypto import decrypt_secret
from daalu_automation.core.keycloak import get_machine_token

logger = structlog.get_logger(__name__)

# The Temporal stage that pauses for human approval in NV-CM's DeployWorkflow
# (verified in temporal/ngc/workflows/deploy.py). The hub approves it as the
# already-approved executor (the human approved in the daalu UI first).
DEPLOY_APPROVAL_STAGE = "perform_configuration_diff"


class NvcmClientError(RuntimeError):
    """Raised on a non-2xx response or transport failure from an NV-CM service."""


@dataclass(frozen=True)
class NvcmConn:
    """Resolved connection params for one tenant's NV-CM stack.

    ``verify`` is the TLS verify flag/CA-bundle path; the per-tenant gateway
    certs come from cert-manager, so in production this is ``True`` (or a CA
    path). Set ``False`` only for local/self-signed dev.
    """

    config_store_url: str
    render_url: str
    workflow_url: str
    keycloak_client_id: str
    keycloak_client_secret: str
    audience: str | None = None
    verify: bool | str = True
    # When set (``http://<tunnel_ip>:8888``), svc-* calls are dialed through
    # the tenant's daalu-edge forward proxy over WireGuard instead of
    # directly — required when the tenant's NV-CM runs on a workload cluster
    # whose ``svc-*`` hosts only resolve in-cluster. ``None`` → direct dial
    # (the hub has a route to the gateway). Resolved by the caller from the
    # Integration's ``cluster_tunnel_id`` via ``core.cluster_proxy.get_proxy_url``.
    proxy_url: str | None = None


def conn_from_integration_config(
    config: dict[str, Any], *, proxy_url: str | None = None
) -> NvcmConn:
    """Build an :class:`NvcmConn` from an ``Integration.config`` blob.

    Expected keys (written by the onboarding wizard / controller):
    ``config_store_url``, ``render_url``, ``workflow_url``,
    ``keycloak_client_id``, ``keycloak_client_secret_ciphertext``,
    optional ``keycloak_audience`` and ``tls_verify``.

    ``proxy_url`` (resolved by the caller from the Integration's
    ``cluster_tunnel_id``) routes the svc-* calls through the tenant's edge
    forward proxy over WireGuard. The tunnel is a row column, not part of the
    config blob, so it's passed in explicitly.
    """
    secret_ct = config.get("keycloak_client_secret_ciphertext")
    secret = decrypt_secret(secret_ct) if secret_ct else ""
    return NvcmConn(
        config_store_url=str(config["config_store_url"]).rstrip("/"),
        render_url=str(config["render_url"]).rstrip("/"),
        workflow_url=str(config["workflow_url"]).rstrip("/"),
        keycloak_client_id=str(config.get("keycloak_client_id", "")),
        keycloak_client_secret=secret,
        audience=config.get("keycloak_audience"),
        verify=config.get("tls_verify", True),
        proxy_url=proxy_url,
    )


class _BaseClient:
    """Shared httpx + auth machinery for the three NV-CM service clients."""

    def __init__(self, conn: NvcmConn, base_url: str) -> None:
        self._conn = conn
        self._base_url = base_url.rstrip("/")

    async def _headers(self) -> dict[str, str]:
        token = await get_machine_token(
            client_id=self._conn.keycloak_client_id,
            client_secret=self._conn.keycloak_client_secret,
            audience=self._conn.audience,
        )
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def _request(
        self, method: str, path: str, *, json: Any | None = None
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        headers = await self._headers()
        try:
            async with httpx.AsyncClient(
                timeout=60.0, verify=self._conn.verify, proxy=self._conn.proxy_url
            ) as client:
                resp = await client.request(method, url, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise NvcmClientError(f"{method} {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise NvcmClientError(
                f"{method} {url} returned {resp.status_code}: {resp.text[:300]}"
            )
        return resp


class ConfigStoreClient(_BaseClient):
    """Versioned config storage — write/read intended config + diff."""

    def __init__(self, conn: NvcmConn) -> None:
        super().__init__(conn, conn.config_store_url)

    async def put_intended(
        self,
        device_uuid: str,
        filename: str,
        content: str,
        *,
        author: str = "daalu-automation",
        commit_message: str = "staged by daalu-automation",
        file_type: str = "intended",
    ) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            f"/v1/config/{device_uuid}/{filename}",
            json={
                "content": content,
                "author": author,
                "commit_message": commit_message,
                "file_type": file_type,
            },
        )
        return resp.json()

    async def get_config(
        self,
        device_uuid: str,
        filename: str,
        *,
        file_type: str = "intended",
        version: int | None = None,
    ) -> dict[str, Any]:
        path = f"/v1/config/{device_uuid}/{filename}?file_type={file_type}"
        if version is not None:
            path += f"&version={version}"
        return (await self._request("GET", path)).json()

    async def list_versions(
        self, device_uuid: str, filename: str, *, file_type: str = "intended"
    ) -> dict[str, Any]:
        return (
            await self._request(
                "GET",
                f"/v1/config/{device_uuid}/{filename}/versions?file_type={file_type}",
            )
        ).json()

    async def diff(
        self,
        device_uuid: str,
        filename: str,
        from_version: int,
        to_version: int,
        *,
        file_type: str = "intended",
    ) -> dict[str, Any]:
        return (
            await self._request(
                "GET",
                f"/v1/config/{device_uuid}/{filename}/diff"
                f"?from_version={from_version}&to_version={to_version}"
                f"&file_type={file_type}",
            )
        ).json()


class RenderClient(_BaseClient):
    """Trigger NV-CM's Jinja2 render for a device on demand."""

    def __init__(self, conn: NvcmConn) -> None:
        super().__init__(conn, conn.render_url)

    async def render_device(
        self, device_uuid: str, *, commit_message: str = "rendered by daalu-automation"
    ) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                f"/v1/render/{device_uuid}/render",
                json={"commit_message": commit_message},
            )
        ).json()


class TemporalWorkflowClient(_BaseClient):
    """Start + drive NV-CM Temporal workflows (deploy, approve, status)."""

    def __init__(self, conn: NvcmConn) -> None:
        super().__init__(conn, conn.workflow_url)

    async def start_deploy(
        self, device_id: str, *, commit_confirm: bool = True
    ) -> dict[str, Any]:
        return (
            await self._request(
                "POST",
                "/v1/workflow/ngc/deploy",
                json={"device_id": device_id, "commit_confirm": commit_confirm},
            )
        ).json()

    async def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/v1/workflow/{workflow_id}")).json()

    async def approve_stage(
        self, workflow_id: str, stage_name: str = DEPLOY_APPROVAL_STAGE
    ) -> dict[str, Any]:
        return (
            await self._request(
                "POST", f"/v1/workflow/{workflow_id}/approve/{stage_name}"
            )
        ).json()

    async def reject_stage(
        self, workflow_id: str, stage_name: str = DEPLOY_APPROVAL_STAGE
    ) -> dict[str, Any]:
        return (
            await self._request(
                "POST", f"/v1/workflow/{workflow_id}/reject/{stage_name}"
            )
        ).json()
