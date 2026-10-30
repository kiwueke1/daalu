"""Health precheck for a tenant's Tinkerbell mgmt cluster.

Resolves the tenant's ``Integration(provider="tinkerbell")`` target and
probes it for reachability (API server up over the tunnel + Tinkerbell
CRDs installed). Two callers:

* the per-integration health beat (``workers.integration_health``), so a
  broken tinkerbell wiring flips the Integrations-UI badge to ``error``
  within a tick — failures surface at *onboarding*, not at execute time
  (design open-question #5, engineer chapter 64 §64.6).
* ``change_proposals.execute_provision`` as a fail-fast guard before it
  drives a ``provision_op``.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.tinkerbell.client import TinkerbellClient
from daalu_automation.core.tinkerbell.target import resolve_tinkerbell_target

logger = structlog.get_logger(__name__)


async def check_health(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[bool, str]:
    """Return ``(ok, message)`` for the tenant's Tinkerbell integration.

    ``ok=False`` with a human message when the integration is missing /
    misconfigured / unreachable; never raises.
    """
    try:
        kubeconfig, namespace = await resolve_tinkerbell_target(db, tenant_id)
    except LookupError as e:
        return False, str(e)
    try:
        async with TinkerbellClient(kubeconfig=kubeconfig, namespace=namespace) as tk:
            await tk.probe()
    except Exception as e:  # noqa: BLE001 — any failure is "not healthy"
        logger.info(
            "tinkerbell.health.unreachable",
            tenant_id=str(tenant_id),
            error=str(e),
        )
        return False, f"{type(e).__name__}: {e}"[:500]
    return True, "ok"
