"""Drift reconciler — Celery beat task.

Every ``settings.sot_reconcile_period_s`` seconds, fan out across every
non-deleted tenant; for each tenant with a Nautobot integration and a
tenant-wide SSH credentials row, walk the Linux devices in the SoT and
compare observed → intended. Open a ``ChangeProposal(kind="drift")``
when they diverge — unless one is already pending or approved for the
same device, in which case skip (we don't want to spam approvers).

Tenants without nautobot or ssh_credentials configured are silently
skipped. Per-device errors are logged and don't stop the per-tenant
sweep.

For v1 the fan-out is serial. The plan calls this acceptable up to
tens of devices per tenant; per-tenant Celery chain splits are a PR-2
concern.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import or_, select

from daalu_automation.core import change_proposals as cps
from daalu_automation.core.device import get_device_adapter
from daalu_automation.core.events import EventEnvelope, publish
from daalu_automation.core.sot import NautobotSoT, NautobotUnavailable
from daalu_automation.core.sot.models import Actor
from daalu_automation.database import AsyncSessionLocal, engine
from daalu_automation.models import (
    ChangeProposal,
    ChangeProposalKind,
    ChangeProposalStatus,
    Integration,
    Tenant,
)
from daalu_automation.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _eligible_tenant_ids() -> list[uuid.UUID]:
    """Tenants with both nautobot AND ssh_credentials integrations."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Tenant.id).where(Tenant.is_deleted.is_(False))
            )
        ).all()
        tenant_ids = [r[0] for r in rows]
        if not tenant_ids:
            return []
        # Per-tenant gate: must have a nautobot row AND an ssh_credentials row.
        integ_rows = (
            await db.execute(
                select(Integration.tenant_id, Integration.provider).where(
                    Integration.tenant_id.in_(tenant_ids),
                    or_(
                        Integration.provider == "nautobot",
                        Integration.provider == "config_manager",
                        Integration.provider == "ssh_credentials",
                    ),
                )
            )
        ).all()
    by_tenant: dict[uuid.UUID, set[str]] = {}
    for tid, provider in integ_rows:
        by_tenant.setdefault(tid, set()).add(provider)
    # A tenant needs SSH credentials plus *some* Nautobot. "Some" is broader
    # than the BYO `nautobot` row: an NV-CM tenant's Nautobot arrives bundled
    # with a management environment and is reached via the `config_manager`
    # integration, so gating on `nautobot` alone skipped those tenants
    # entirely — no drift reconciliation at all, however many devices they had
    # onboarded.
    return [
        tid for tid, providers in by_tenant.items()
        if "ssh_credentials" in providers
        and providers & {"nautobot", "config_manager"}
    ]


async def _environments_for(
    db, tenant_id: uuid.UUID
) -> list[str | None]:
    """The environment slugs whose fabrics this tenant needs swept.

    Each NV-CM management environment bundles its own Nautobot and so has its
    own device inventory. Returns one entry per environment that has a
    Nautobot URL published, or ``[None]`` — the tenant-default resolution —
    for a BYO tenant with no environments at all.

    The primary environment is *not* also returned as ``None``: with a
    ``config_manager`` integration present the default resolution already
    lands on the primary's Nautobot, so including both would sweep the same
    devices twice.
    """
    from daalu_automation.models.config_manager_tenant import ConfigManagerTenant

    rows = (
        await db.execute(
            select(
                ConfigManagerTenant.slug, ConfigManagerTenant.urls
            ).where(ConfigManagerTenant.tenant_id == tenant_id)
        )
    ).all()
    slugs = [
        slug for slug, urls in rows if (urls or {}).get("nautobot_url")
    ]
    return list(slugs) if slugs else [None]


async def _has_open_proposal(
    db, tenant_id: uuid.UUID, device_id: str, environment_slug: str | None
) -> bool:
    open_statuses = (
        ChangeProposalStatus.pending,
        ChangeProposalStatus.approved,
    )
    row = (
        await db.execute(
            select(ChangeProposal.id).where(
                ChangeProposal.tenant_id == tenant_id,
                ChangeProposal.device_id == device_id,
                # Device ids are unique only within an environment, so an open
                # proposal in one fabric must not mask drift in another.
                ChangeProposal.environment_slug.is_(None)
                if environment_slug is None
                else ChangeProposal.environment_slug == environment_slug,
                ChangeProposal.status.in_(open_statuses),
            ).limit(1)
        )
    ).first()
    return row is not None


async def _reconcile_tenant(tenant_id: uuid.UUID) -> dict[str, Any]:
    """Sweep every one of the tenant's fabrics, one environment at a time.

    A failure inside one environment (an unreachable Nautobot, a
    half-provisioned stack) is confined to that environment — the remaining
    ones are still swept.
    """
    adapter = get_device_adapter("linux_ssh")
    stats = {
        "environments": 0,
        "devices": 0,
        "drift_proposals": 0,
        "errors": 0,
        "skipped_open": 0,
    }
    async with AsyncSessionLocal() as db:
        for env in await _environments_for(db, tenant_id):
            stats["environments"] += 1
            await _reconcile_environment(db, tenant_id, env, adapter, stats)
    return stats


async def _reconcile_environment(
    db,
    tenant_id: uuid.UUID,
    env: str | None,
    adapter: Any,
    stats: dict[str, Any],
) -> None:
    sot = NautobotSoT(env=env)
    try:
        devices = await sot.list_devices(db, tenant_id, platform="linux")
    except NautobotUnavailable as e:
        logger.warning(
            "reconciler.nautobot_unavailable",
            extra={
                "tenant_id": str(tenant_id),
                "environment": env,
                "error": str(e),
            },
        )
        return

    for device in devices:
        stats["devices"] += 1
        try:
            if await _has_open_proposal(db, tenant_id, device.id, env):
                stats["skipped_open"] += 1
                continue
            intended = await sot.get_intended_config(db, tenant_id, device.id)
            if intended is None:
                continue
            creds = await cps.resolve_credentials(db, tenant_id, device)
            observed = await adapter.collect(creds, intended_hint=intended.facts)
            diff = await adapter.diff(observed, intended.facts)
            if not diff.has_changes:
                await publish(EventEnvelope(
                    tenant_id=str(tenant_id),
                    type="device.observed.snapshot",
                    module="sot",
                    source="reconciler",
                    severity="info",
                    summary=f"device {device.name} in sync",
                    payload={
                        "device_id": device.id,
                        "environment_slug": env,
                        "facts_changed": [],
                    },
                ))
                continue
            rendered_intended = await adapter.render(intended.facts)
            rendered_observed = await adapter.render(observed)
            await cps.propose(
                db,
                tenant_id,
                device_id=device.id,
                environment_slug=env,
                kind=ChangeProposalKind.drift,
                intended_config=cps.serialize_rendered_files(
                    rendered_intended.files
                ),
                observed_config=cps.serialize_rendered_files(
                    rendered_observed.files
                ),
                diff=diff.unified_diff,
                renderer_version=rendered_intended.renderer_version,
                evidence={
                    "triggered_by": "reconciler",
                    "confidence": 1.0,
                    "facts_changed": diff.facts_changed,
                },
                actor=Actor(kind="system", name="reconciler"),
            )
            stats["drift_proposals"] += 1
        except Exception as e:  # noqa: BLE001 — per-device boundary
            stats["errors"] += 1
            logger.exception(
                "reconciler.device_failed",
                extra={
                    "tenant_id": str(tenant_id),
                    "environment": env,
                    "device_id": device.id,
                    "error": f"{type(e).__name__}: {e}",
                },
            )


async def _reconcile_all() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for tid in await _eligible_tenant_ids():
        out[str(tid)] = await _reconcile_tenant(tid)
    return out


@celery_app.task(name="sot.reconcile_devices")
def reconcile_devices_task() -> dict[str, Any]:
    # Same dispose-on-tick pattern as wireguard_health: the async engine
    # pins its pool to the first event loop, but asyncio.run() creates a
    # fresh loop each tick. Dispose at end-of-tick → fresh pool next
    # tick. ~one reconnect per period — fine for 5min cadence.
    async def _wrapped() -> dict[str, Any]:
        try:
            return await _reconcile_all()
        finally:
            await engine.dispose()
    return asyncio.run(_wrapped())
