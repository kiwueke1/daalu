"""Resolve which shared GPU pool serves a gateway request.

Replaces the single static ``settings.daalu_hosted_upstream_url`` with a
lookup over the ``gpu_pools`` registry (doc 13). Today there is at most one
enabled pool (the operator's RTX 6000 Ada), so this is a trivial selection;
the seam is here so multi-provider load-balancing (round-robin / home-pool /
model-match) slots in without touching the request path.

Falls back to ``settings.daalu_hosted_upstream_url`` when no pool row exists
(bootstrap / manual override), in which case the call has no provider to
credit and ``record_revenue_share`` is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import get_settings
from daalu_automation.models.gpu_pool import GpuPool

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class ResolvedUpstream:
    """Where a request goes and who to credit for it.

    ``pool_id`` / ``provider_tenant_id`` are None for the env-var fallback —
    no ledger row is written in that case.
    """

    upstream_url: str
    pool_id: str | None
    provider_tenant_id: str | None


async def resolve_upstream(db: AsyncSession, model: str | None) -> ResolvedUpstream:
    """Pick an enabled pool that serves ``model``; else env fallback.

    Selection policy (matters once a *second* model/provider exists, e.g.
    the coder on the RTX 6000 alongside the classifier):

    1. Prefer a pool that lists ``model`` explicitly in ``served_models``.
       This stops an empty-list catch-all from silently swallowing a coder
       request and proxying it to a vLLM that 404s on that model id.
    2. Only if none matches explicitly, fall back to a catch-all pool
       (empty ``served_models`` = "serves any model").
    3. Within each tier, oldest pool wins (deterministic ``created_at``
       order) — no behaviour drift between requests.

    With no pool rows at all, fall back to ``daalu_hosted_upstream_url``
    (single-model bootstrap: the coder is the only upstream).
    """
    rows = (
        (
            await db.execute(
                select(GpuPool)
                .where(GpuPool.enabled.is_(True))
                .order_by(GpuPool.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    catch_all: GpuPool | None = None
    for pool in rows:
        served = pool.served_models or []
        if model is not None and model in served:
            return ResolvedUpstream(
                upstream_url=pool.upstream_url,
                pool_id=str(pool.id),
                provider_tenant_id=str(pool.provider_tenant_id),
            )
        if not served and catch_all is None:
            catch_all = pool

    if catch_all is not None:
        return ResolvedUpstream(
            upstream_url=catch_all.upstream_url,
            pool_id=str(catch_all.id),
            provider_tenant_id=str(catch_all.provider_tenant_id),
        )

    fallback = get_settings().daalu_hosted_upstream_url
    if not fallback:
        logger.warning("gateway.no_pool_and_no_fallback", model=model)
    return ResolvedUpstream(
        upstream_url=fallback, pool_id=None, provider_tenant_id=None
    )
