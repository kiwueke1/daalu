"""Celery tasks for the infra module.

The beat schedule fires these tasks at fixed cron times; the tasks
themselves fan out over every live tenant. The scheduler only knows
*when* to run — the *for whom* mapping lives in the ``tenants`` table.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from daalu_automation.database import AsyncSessionLocal, engine
from daalu_automation.models import Tenant
from daalu_automation.modules.infra.briefing import InfraBriefingGenerator
from daalu_automation.workers.celery_app import celery_app


async def _all_tenant_ids() -> list:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Tenant.id).where(Tenant.is_deleted.is_(False))
            )
        ).all()
    return [r[0] for r in rows]


async def _generate_for_all() -> list[str]:
    out: list[str] = []
    for tid in await _all_tenant_ids():
        briefing = await InfraBriefingGenerator().generate(tenant_id=tid)
        out.append(str(briefing.id))
    return out


@celery_app.task(name="infra.generate_briefing")
def generate_briefing_task() -> list[str]:
    # See poll_tunnel_health_task for the rationale: SQLAlchemy's async
    # engine pins its connection pool to the first event loop that
    # touches it. asyncio.run() creates a fresh loop every tick; without
    # dispose() the pool leaks server-side connections every tick and
    # Postgres exhausts max_connections within a day.
    async def _wrapped() -> list[str]:
        try:
            return await _generate_for_all()
        finally:
            await engine.dispose()
    return asyncio.run(_wrapped())


async def _ingest_for_all(provider: str) -> int:
    from daalu_automation.core.integrations import get_integration

    total = 0
    adapter = get_integration(provider)
    for tid in await _all_tenant_ids():
        total += await adapter.ingest(tid)
    return total


@celery_app.task(name="infra.monitoring_ingest")
def monitoring_ingest_task(provider: str = "synthetic-infra") -> int:
    # Same engine-leak fix as generate_briefing_task — without this,
    # every 60s tick (per provider) leaks asyncpg connections bound to
    # a now-dead event loop and the pool fills to max_connections.
    async def _wrapped() -> int:
        try:
            return await _ingest_for_all(provider)
        finally:
            await engine.dispose()
    return asyncio.run(_wrapped())
