"""Read NV-CM version state and schedule chart upgrades.

Companion to ``provision.py``. Kept separate because these calls are the
*non*-blocking half of the controller surface: the UI polls status on an
interval and fires an upgrade that returns immediately, rather than holding a
request open for the length of a Helm rollout.

See docs/design/nv-config-manager-integration.md §7.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from daalu_automation.config import get_settings
from daalu_automation.core.configmgr.provision import ConfigManagerProvisioningError


@dataclass
class ConfigManagerVersionStatus:
    state: str
    chart_version: str
    latest_chart_version: str | None
    available_chart_versions: list[str]
    upgrade_available: bool
    last_error: str | None


def _headers(tenant_id: uuid.UUID) -> dict[str, str]:
    from daalu_automation.core.service_tokens import mint_service_token

    token = mint_service_token(
        tenant_id=str(tenant_id),
        user_id=str(tenant_id),
        purpose="config-manager-provision",
        ttl_seconds=300,
    )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    s = get_settings()
    if not s.config_manager_controller_url:
        raise ConfigManagerProvisioningError(
            "config_manager_controller_url is not configured on this deploy"
        )
    return s.config_manager_controller_url.rstrip("/")


def _to_status(view: dict[str, Any]) -> ConfigManagerVersionStatus:
    return ConfigManagerVersionStatus(
        state=view.get("state", "unknown"),
        chart_version=view.get("chart_version") or "",
        latest_chart_version=view.get("latest_chart_version"),
        available_chart_versions=list(view.get("available_chart_versions") or []),
        upgrade_available=bool(view.get("upgrade_available")),
        last_error=view.get("last_error"),
    )


async def get_version_status(
    *, tenant_id: uuid.UUID
) -> ConfigManagerVersionStatus | None:
    """Current + newest-available chart version, or ``None`` if no stack yet."""
    base = _base_url()
    async with httpx.AsyncClient(timeout=15.0, headers=_headers(tenant_id)) as client:
        resp = await client.get(f"{base}/tenants/{tenant_id}")
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise ConfigManagerProvisioningError(
            f"controller status failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    return _to_status(resp.json())


async def schedule_upgrade(
    *, tenant_id: uuid.UUID, chart_version: str | None = None
) -> ConfigManagerVersionStatus:
    """Ask the controller to re-pin + re-provision; returns immediately.

    The rollout itself happens in the controller's reconcile loop, so callers
    poll :func:`get_version_status` rather than awaiting completion here — a
    Helm rollout comfortably outlives the edge proxy's connection ceiling.
    """
    base = _base_url()
    body: dict[str, Any] = {}
    if chart_version:
        body["chart_version"] = chart_version
    async with httpx.AsyncClient(timeout=30.0, headers=_headers(tenant_id)) as client:
        resp = await client.post(f"{base}/tenants/{tenant_id}/upgrade", json=body)
    if resp.status_code == 404:
        raise ConfigManagerProvisioningError("no NV-CM stack provisioned for this tenant")
    if resp.status_code != 200:
        # 400/409 carry an actionable reason (unknown version, not newer,
        # mid-teardown) — surface it rather than a generic failure.
        detail = resp.text[:300]
        try:
            detail = resp.json().get("detail", detail)
        except Exception:  # noqa: BLE001
            pass
        raise ConfigManagerProvisioningError(str(detail))
    return _to_status(resp.json())
