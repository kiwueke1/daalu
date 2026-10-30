"""Per-integration health-check beat task.

Every tick (default: 60 s), walk every ``Integration`` row and call its
adapter's ``health(tenant_id)`` probe. Persist the result on the row so
the Integrations UI can render the current liveness without any other
state:

- ``Integration.status``         — ``connected`` on ok, ``error`` on miss
- ``Integration.last_probed_at`` — wall-clock when this tick ran
- ``Integration.last_error``     — adapter's error message on miss

Adapters that don't override ``health`` keep the base-class default of
``(True, "ok")`` and stay green. The two adapters that matter most for
the current product surface — Prometheus and PagerDuty — override with
real probes (see ``modules/infra/integrations.py``).

Why this exists. The previous design only set ``Integration.status``
once, at onboarding time, and never flipped it. If a customer's
Prometheus went offline, the badge stayed green forever. With this
task in place the badge flips within one tick (worst-case 60 s).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.integrations import get_integration
from daalu_automation.database import AsyncSessionLocal, engine
from daalu_automation.models import Integration, IntegrationStatus
from daalu_automation.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)


async def _probe_one(db: AsyncSession, row: Integration) -> tuple[bool, str]:
    """Resolve the adapter for ``row.provider`` and run its probe.

    Falls back to ``(False, "unknown provider")`` if the registry has
    no entry — that surfaces stale rows after a module rename instead
    of silently flagging them connected.
    """
    # Adapter-less providers with a bespoke probe. Tinkerbell has no
    # IntegrationAdapter (it's driven via CRDs over the tunnel), so probe
    # the mgmt cluster directly instead of reporting "unknown provider".
    if row.provider == "tinkerbell":
        from daalu_automation.core.tinkerbell.health import check_health

        return await check_health(db, row.tenant_id)
    # config_manager has no IntegrationAdapter either — the per-tenant NV-CM
    # stack is owned by config-manager-controller, whose ConfigManagerTenant
    # row is the authoritative health signal. Mirror it instead of reporting
    # "unknown provider" (which would flag the badge `error` forever).
    if row.provider == "config_manager":
        from daalu_automation.core.configmgr.health import check_health

        return await check_health(db, row.tenant_id)
    # kubernetes has no IntegrationAdapter either — the row owns the tenant's
    # kubeconfig + cluster tunnel (models/cluster_tunnel.py), and L3+API
    # reachability *is* the product signal the agent depends on. Probe by
    # listing nodes through the stored kubeconfig: a 200 proves both the tunnel
    # and API auth. Without this branch the beat task reports "unknown provider:
    # kubernetes" and pins the badge red even while the cluster is reachable.
    # grafana has no IntegrationAdapter — dashboards are served directly via
    # core/grafana.py, not the alert/metrics adapter surface. Without this
    # branch the beat task reports "unknown provider: grafana" and pins the
    # badge red even while dashboards render fine. Probe the search API (proves
    # reachability + token auth).
    if row.provider == "grafana":
        from daalu_automation.core.grafana import health as grafana_health

        return await grafana_health(row.tenant_id)
    if row.provider == "kubernetes":
        from daalu_automation.core.kube_tools import _tenant_kube_or_default

        try:
            core_v1, _apps, _client = await _tenant_kube_or_default(row.tenant_id)
            nodes = await asyncio.to_thread(lambda: core_v1.list_node().items)
            return True, f"ok ({len(nodes)} nodes)"
        except Exception as e:  # noqa: BLE001 — surface any reachability failure
            return False, str(e)[:500]
    try:
        adapter = get_integration(row.provider)
    except KeyError:
        return False, f"unknown provider: {row.provider}"
    try:
        return await adapter.health(row.tenant_id)
    except Exception as e:  # noqa: BLE001 — last-line guard for adapter bugs
        logger.warning(
            "integration.health.adapter_error",
            provider=row.provider,
            tenant_id=str(row.tenant_id),
            error=str(e),
        )
        return False, f"probe raised: {type(e).__name__}: {e}"[:500]


async def _check_all() -> dict[str, int]:
    """One pass: probe every Integration row, update status fields.

    Returns ``{checked, flipped}`` for the celery return value so the
    output is greppable in celery events.
    """
    now = datetime.now(tz=timezone.utc)
    checked = 0
    flipped = 0

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Integration))).scalars().all()
        for row in rows:
            ok, msg = await _probe_one(db, row)
            new_status = (
                IntegrationStatus.connected if ok else IntegrationStatus.error
            )
            if row.status != new_status:
                flipped += 1
                logger.info(
                    "integration.health.flip",
                    provider=row.provider,
                    tenant_id=str(row.tenant_id),
                    old=row.status.value,
                    new=new_status.value,
                    reason=msg,
                )
            row.status = new_status
            row.last_probed_at = now
            # Clear last_error on recovery; otherwise the UI keeps
            # showing the old failure forever.
            row.last_error = None if ok else msg[:1024]
            checked += 1
        await db.commit()

    return {"checked": checked, "flipped": flipped}


@celery_app.task(name="integrations.health_check")
def health_check_task() -> dict[str, int]:
    """Beat-driven entrypoint.

    Engine disposed after each tick — see poll_tunnel_health_task for
    the asyncio.run + module-level engine leak rationale.
    """
    async def _wrapped() -> dict[str, int]:
        try:
            return await _check_all()
        finally:
            await engine.dispose()
    return asyncio.run(_wrapped())
