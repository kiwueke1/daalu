"""Health signal for a tenant's NV-CM (`config_manager`) integration.

The per-tenant NV-CM stack is provisioned and owned by
``config-manager-controller``; its ``ConfigManagerTenant`` row is the
authoritative health signal (the controller's reconcile loop tracks the helm
release + Tier-A prechecks there). The `config_manager` Integration has no
``IntegrationAdapter``, so the per-integration health beat
(``workers.integration_health``) mirrors that row instead of reporting
"unknown provider" — which would otherwise flag the Integrations-UI badge
``error`` forever. Never raises.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.models import ConfigManagerTenant

logger = structlog.get_logger(__name__)


async def check_health(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[bool, str]:
    """Return ``(ok, message)`` for the tenant's NV-CM stack.

    ``ok`` iff the controller row reports the stack is up. ``active`` and the
    transient ``provisioning`` (a re-converge in flight) both count as healthy
    so the badge doesn't flap; ``error`` surfaces the controller's last error.
    """
    try:
        row = (
            await db.execute(
                select(ConfigManagerTenant).where(
                    ConfigManagerTenant.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
    except Exception as e:  # pragma: no cover - DB hiccup, treat as unknown
        return False, f"could not read NV-CM stack state: {type(e).__name__}"

    if row is None:
        return False, "no NV-CM stack provisioned for this tenant"

    state = row.state.value if hasattr(row.state, "value") else str(row.state)
    if state == "active":
        return True, "NV-CM stack active"
    # A stack that has been ready at least once and is mid-reconcile is still
    # usable — don't flap the badge on the controller's steady-state churn.
    if state == "provisioning" and row.last_ready_at is not None:
        return True, "NV-CM stack active (re-converge in progress)"
    return False, f"NV-CM stack {state}: {(row.last_error or '').strip()[:300]}".strip()
