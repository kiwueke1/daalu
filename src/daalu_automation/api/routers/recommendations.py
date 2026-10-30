from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id
from daalu_automation.api.schemas import RecommendationOut
from daalu_automation.database import get_db
from daalu_automation.models import Recommendation, RecommendationStatus

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


@router.get("", response_model=list[RecommendationOut])
async def list_recommendations(
    module: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    stmt = (
        select(Recommendation)
        .where(Recommendation.tenant_id == tenant_id)
        .order_by(desc(Recommendation.created_at))
        .limit(limit)
    )
    if module:
        stmt = stmt.where(Recommendation.module == module)
    if status:
        stmt = stmt.where(Recommendation.status == status)
    return (await db.execute(stmt)).scalars().all()


async def _get_tenant_rec(
    db: AsyncSession, rec_id: str, tenant_id
) -> Recommendation:
    """Fetch a recommendation scoped to the caller's tenant — 404 on miss.

    Tenant mismatch returns 404 (not 403) so cross-tenant UUID probing
    can't even confirm a row exists.
    """
    stmt = select(Recommendation).where(
        Recommendation.id == rec_id,
        Recommendation.tenant_id == tenant_id,
    )
    rec = (await db.execute(stmt)).scalar_one_or_none()
    if rec is None:
        raise HTTPException(404, "recommendation not found")
    return rec


@router.post("/{rec_id}/approve", response_model=RecommendationOut)
async def approve_recommendation(
    rec_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    rec = await _get_tenant_rec(db, rec_id, tenant_id)
    rec.status = RecommendationStatus.approved
    await db.commit()
    await db.refresh(rec)
    return rec


@router.post("/{rec_id}/dismiss", response_model=RecommendationOut)
async def dismiss_recommendation(
    rec_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    rec = await _get_tenant_rec(db, rec_id, tenant_id)
    rec.status = RecommendationStatus.dismissed
    await db.commit()
    await db.refresh(rec)
    return rec
