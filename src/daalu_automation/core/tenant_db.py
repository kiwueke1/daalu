"""Per-tenant DB session resolution.

Daalu Private's "customer-managed Postgres" mode means tenant-scoped
tables (events, alerts, change_proposals, …) live in a Postgres the
customer operates, not the hub DB. This module is the single
chokepoint where the rest of the codebase asks: "give me a session
for tenant X's data."

For Standard tenants the call returns a session against the hub DB
(same as ``database.get_db``). For Private tenants with
``private_db_url`` set the call returns a session against the
customer's DB.

**Current state (Phase 1 scaffolding):**

Returns the hub session unconditionally. The TODO under the
``_resolve_engine`` placeholder is the production-grade
implementation — engine cache, secret resolution, health probing,
etc. We deliberately ship the chokepoint *now* so the rest of the
code can adopt it; the routing brain swaps in later without a
call-site change.

See ``docs/design/daalu-private.md`` §4 ("What lives where") for
the table split, and §10 for the production TODOs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from daalu_automation.database import AsyncSessionLocal

logger = structlog.get_logger(__name__)

# Engine cache. Each unique URL gets one engine; tenants pointing at
# the same DB share. Populated lazily by ``_resolve_engine``.
_engine_cache: dict[str, AsyncEngine] = {}


async def _resolve_engine(private_db_url: str) -> AsyncEngine:  # noqa: ARG001
    """Return (or create + cache) the async engine for a Private DB URL.

    TODO (Phase 3 — production):
        * Pull the bearer token from a K8s Secret named per-tenant
          (same pattern as SOVEREIGN tokens).
        * Build the URL string with the resolved credentials.
        * ``create_async_engine`` with pool_size sized to expected
          tenant concurrency.
        * Probe the engine on first use and cache health for ~30 s.
    """
    raise NotImplementedError(
        "Private-DB engine resolution is not yet implemented — see "
        "docs/design/daalu-private.md §10. Set tenant.is_private=false "
        "or tenant.private_db_url=NULL to fall back to the hub DB."
    )


@asynccontextmanager
async def get_tenant_session(tenant) -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession against the right DB for ``tenant``.

    ``tenant`` is a :class:`models.Tenant` row already loaded by the
    caller from the hub DB.

    Standard tenants → hub session.
    Private tenants with ``private_db_url`` → customer DB session
    (NOT YET IMPLEMENTED — falls back to hub with a warning so the
    caller doesn't break in pre-release dogfooding).
    """
    if getattr(tenant, "is_private", False) and getattr(tenant, "private_db_url", None):
        logger.warning(
            "tenant_db.private_router_not_implemented",
            tenant_id=str(tenant.id),
            falling_back_to="hub",
        )
        # Intentionally falling through: Phase 1 scaffolding. When
        # _resolve_engine is implemented, the fall-through is removed
        # and this branch yields the tenant engine's session.
    async with AsyncSessionLocal() as session:
        yield session
