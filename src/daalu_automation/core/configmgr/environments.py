"""Drive the config-manager-controller's *named environment* surface.

Companion to ``provision.py`` (which addresses a tenant's single, primary
stack) and ``upgrades.py``. Same transport and service-token identity — these
routes just address one environment out of the several a customer may run.

Every call here is non-blocking for the same reason the singular ones are: a
full NV-CM rollout runs far past the edge proxy's ~100 s connection ceiling,
so callers create/upgrade and then poll :func:`environment_progress`.

See docs/design/nv-config-manager-integration.md §7.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

# Token minting and base-URL resolution are shared with the singular routes on
# purpose: the controller accepts one service-token purpose for all of them, so
# a second copy here could only ever drift out of agreement with it.
from daalu_automation.core.configmgr.provision import (
    ConfigManagerProvisioningError,
    _auth_headers,
    _controller_base,
)


class ConfigManagerRequestError(ConfigManagerProvisioningError):
    """A controller rejection that carries an actionable, user-safe reason.

    The controller answers 400 (the name can't be a slug) and 409 (that slug
    is taken) with detail written to be shown to the person who typed the
    name. Keeping the status alongside the text lets the hub route relay both
    verbatim instead of flattening everything into a generic 502.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _detail(resp: httpx.Response) -> str:
    """Best-effort extraction of FastAPI's ``{"detail": …}`` body."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return resp.text[:300]
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail:
        return detail
    return resp.text[:300]


async def list_environments(*, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every environment this tenant has, primary first."""
    base = _controller_base()
    async with httpx.AsyncClient(
        timeout=20.0, headers=_auth_headers(tenant_id)
    ) as client:
        resp = await client.get(f"{base}/tenants/{tenant_id}/environments")
    if resp.status_code != 200:
        raise ConfigManagerRequestError(resp.status_code, _detail(resp))
    return list(resp.json() or [])


async def create_environment(
    *,
    tenant_id: uuid.UUID,
    name: str,
    components: dict[str, bool] | None = None,
    size_profile: str = "small",
    chart_version: str | None = None,
    target_cluster_tunnel_id: uuid.UUID | None = None,
    admin_username: str | None = None,
    admin_password: str | None = None,
) -> dict[str, Any]:
    """Create an environment and queue its install; returns immediately.

    The controller derives the slug, namespace and hostnames from ``name`` —
    the caller deliberately cannot set them, since all three are baked into
    live URLs and are immutable afterwards.

    ``admin_username``/``admin_password`` set the break-glass Nautobot login
    (the only NV-CM tool with one). Both optional; the controller defaults the
    username and generates the password, and returns the result once.
    """
    base = _controller_base()
    body: dict[str, Any] = {"name": name, "size_profile": size_profile}
    if components is not None:
        body["components"] = components
    if chart_version:
        body["chart_version"] = chart_version
    if target_cluster_tunnel_id:
        body["target_cluster_tunnel_id"] = str(target_cluster_tunnel_id)
    if admin_username:
        body["admin_username"] = admin_username
    if admin_password:
        body["admin_password"] = admin_password

    async with httpx.AsyncClient(
        timeout=30.0, headers=_auth_headers(tenant_id)
    ) as client:
        resp = await client.post(
            f"{base}/tenants/{tenant_id}/environments", json=body
        )
    if resp.status_code not in (200, 201):
        raise ConfigManagerRequestError(resp.status_code, _detail(resp))
    return resp.json()


async def environment_progress(
    *, tenant_id: uuid.UUID, env_id: uuid.UUID
) -> dict[str, Any] | None:
    """Live install progress for one environment, or ``None`` if it's gone."""
    base = _controller_base()
    async with httpx.AsyncClient(
        timeout=20.0, headers=_auth_headers(tenant_id)
    ) as client:
        resp = await client.get(
            f"{base}/tenants/{tenant_id}/environments/{env_id}/progress"
        )
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise ConfigManagerRequestError(resp.status_code, _detail(resp))
    return resp.json()


async def schedule_environment_upgrade(
    *,
    tenant_id: uuid.UUID,
    env_id: uuid.UUID,
    chart_version: str | None = None,
) -> dict[str, Any]:
    """Re-pin one environment to a newer chart; the rollout happens in the
    controller's reconcile loop, so callers poll rather than await it here."""
    base = _controller_base()
    body: dict[str, Any] = {}
    if chart_version:
        body["chart_version"] = chart_version
    async with httpx.AsyncClient(
        timeout=30.0, headers=_auth_headers(tenant_id)
    ) as client:
        resp = await client.post(
            f"{base}/tenants/{tenant_id}/environments/{env_id}/upgrade", json=body
        )
    if resp.status_code != 200:
        raise ConfigManagerRequestError(resp.status_code, _detail(resp))
    return resp.json()


async def delete_environment(
    *, tenant_id: uuid.UUID, env_id: uuid.UUID, force: bool = False
) -> None:
    """Schedule teardown (helm uninstall) of one environment.

    The controller drops its record once the cluster is clear, so a deleted
    environment stops being listed rather than lingering as a ``destroyed``
    tombstone. ``force`` drops the record without a successful uninstall —
    for an environment whose host cluster is gone and whose teardown can
    therefore never finish.
    """
    base = _controller_base()
    async with httpx.AsyncClient(
        timeout=30.0, headers=_auth_headers(tenant_id)
    ) as client:
        resp = await client.delete(
            f"{base}/tenants/{tenant_id}/environments/{env_id}",
            params={"force": "true"} if force else None,
        )
    if resp.status_code not in (200, 204):
        raise ConfigManagerRequestError(resp.status_code, _detail(resp))
