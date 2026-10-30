"""Billing & SKU REST surface.

The endpoints here power the ``/billing`` tab in the frontend:

* ``GET  /billing/skus``           — full SKU catalog (public-ish; any
                                     authenticated user can see what
                                     they could upgrade to).
* ``GET  /billing/current``        — the tenant's current SKU + the
                                     running totals for the period.
* ``GET  /billing/breakdown``      — by-tier and by-source rollups for
                                     the in-page charts.
* ``GET  /billing/daily``          — per-day cost series for the trend
                                     chart.
* ``GET  /billing/local-gpu``      — is the home NIM healthy right now?
                                     Surfaced as a status pill in the UI.
* ``PUT  /billing/sku``            — change the tenant's SKU.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_admin, current_tenant_id
from daalu_automation.api.schemas import (
    BreakdownRowOut,
    DailyPointOut,
    LocalGpuStatusOut,
    PeriodTotalOut,
    SkuOut,
    TenantSkuChangeRequest,
    TenantSkuOut,
)
from daalu_automation.config import get_settings
from daalu_automation.core.billing import (
    breakdown_by_source,
    breakdown_by_tier,
    current_period_total,
    daily_series,
    get_current_sku,
)
from daalu_automation.core.llm import _local_is_healthy
from daalu_automation.database import get_db
from daalu_automation.models.billing import Sku, TenantSku

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/skus", response_model=list[SkuOut])
async def list_skus(db: AsyncSession = Depends(get_db)):
    stmt = (
        select(Sku)
        .where(Sku.is_active.is_(True))
        .order_by(Sku.display_order, Sku.name)
    )
    return (await db.execute(stmt)).scalars().all()


@router.get("/current")
async def current(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    sku = await get_current_sku(db, tenant_id)
    totals = await current_period_total(db, tenant_id)
    return {
        "sku": SkuOut.model_validate(sku, from_attributes=True) if sku else None,
        "totals": PeriodTotalOut(
            period_start=totals.period_start,
            period_end=totals.period_end,
            events=totals.events,
            prompt_tokens=totals.prompt_tokens,
            completion_tokens=totals.completion_tokens,
            cost_usd=totals.cost_usd,
            base_usd=totals.base_usd,
            included_events=totals.included_events,
            included_events_used=totals.included_events_used,
        ),
    }


@router.get("/breakdown")
async def breakdown(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    by_tier = await breakdown_by_tier(db, tenant_id)
    by_source = await breakdown_by_source(db, tenant_id, limit=10)
    return {
        "by_tier": [
            BreakdownRowOut(
                key=row.key,
                events=row.events,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                cost_usd=row.cost_usd,
            )
            for row in by_tier
        ],
        "by_source": [
            BreakdownRowOut(
                key=row.key,
                events=row.events,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                cost_usd=row.cost_usd,
            )
            for row in by_source
        ],
    }


@router.get("/daily", response_model=list[DailyPointOut])
async def daily(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    points = await daily_series(db, tenant_id, days=min(max(days, 1), 90))
    return [
        DailyPointOut(day=p.day, events=p.events, cost_usd=p.cost_usd) for p in points
    ]


@router.get("/local-gpu", response_model=LocalGpuStatusOut)
async def local_gpu_status(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    """GPU status for the banner.

    Prefers the tenant's onboarded **SOVEREIGN** GPU (vLLM deployed over
    the tunnel) and falls back to the operator-wide LOCAL env tier. The
    operator-LOCAL health result is cached for ``llm_local_health_ttl_s``
    seconds inside ``core/llm._local_is_healthy``.
    """
    from daalu_automation.models import GpuTenant, Tenant

    s = get_settings()
    tenant = await db.get(Tenant, tenant_id)

    if tenant is not None and tenant.sovereign_inference_url:
        gpu_row = (
            await db.execute(
                select(GpuTenant).where(GpuTenant.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        model = tenant.sovereign_model_classifier or s.llm_local_model_classifier
        return LocalGpuStatusOut(
            configured=True,
            healthy=await _probe_openai_endpoint(
                tenant.sovereign_inference_url,
                tenant.sovereign_inference_token_enc,
            ),
            base_url=tenant.sovereign_inference_url,
            model_classifier=model,
            model_quality=tenant.sovereign_model_quality or "",
            source="sovereign",
            state=gpu_row.state.value if gpu_row else None,
        )

    return LocalGpuStatusOut(
        configured=bool(s.llm_local_base_url),
        healthy=await _local_is_healthy() if s.llm_local_base_url else False,
        base_url=s.llm_local_base_url,
        model_classifier=s.llm_local_model_classifier,
        model_quality=s.llm_local_model_quality,
        source="local" if s.llm_local_base_url else "none",
    )


async def _probe_openai_endpoint(base_url: str, token_enc: str | None) -> bool:
    """Best-effort GET ``<base_url>/models`` to decide GPU health.

    Thin alias over :func:`core.gpu.health.probe_openai_endpoint`, which is
    now the single implementation — the GPU card and this pill previously
    disagreed on the same page (one probed, the other never set ``healthy``),
    so they deliberately share one probe and one cache.
    """
    from daalu_automation.core.gpu.health import probe_openai_endpoint

    return await probe_openai_endpoint(base_url, token_enc)


@router.put("/sku", response_model=TenantSkuOut)
async def change_sku(
    req: TenantSkuChangeRequest,
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
    _admin=Depends(current_admin),  # only tenant admins switch plans
):
    """Switch the tenant onto a different SKU.

    Closes the existing ``current=True`` row (sets ``ended_at`` + flips
    ``current`` to false) and inserts a new ``current=True`` row for the
    chosen SKU. The unique partial index on ``tenant_skus`` enforces
    only-one-current-per-tenant at the DB level.
    """
    sku = (
        await db.execute(select(Sku).where(Sku.slug == req.sku_slug, Sku.is_active.is_(True)))
    ).scalar_one_or_none()
    if sku is None:
        raise HTTPException(404, f"unknown or inactive SKU: {req.sku_slug}")

    now = datetime.now(tz=timezone.utc)
    await db.execute(
        update(TenantSku)
        .where(TenantSku.tenant_id == tenant_id, TenantSku.current.is_(True))
        .values(current=False, ended_at=now)
    )
    new_row = TenantSku(
        tenant_id=tenant_id,
        sku_id=sku.id,
        current=True,
        started_at=now,
    )
    db.add(new_row)
    await db.commit()
    await db.refresh(new_row)
    return new_row
