"""AI-Ops metrics and maturity level. docs/design/ai-ops-maturity.md §7, §13."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id, current_user
from daalu_automation.core.aiops import maturity, metrics
from daalu_automation.database import get_db
from daalu_automation.models import User

router = APIRouter(prefix="/aiops", tags=["aiops"])


class AssessmentIn(BaseModel):
    answers: dict[str, bool]


@router.get("/metrics")
async def get_metrics(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> dict[str, Any]:
    return await metrics.compute_metrics(db, tenant_id, days=days)


@router.get("/maturity")
async def get_maturity(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> dict[str, Any]:
    return await maturity.compute(db, tenant_id, days=days)


@router.post("/maturity/assessment")
async def post_assessment(
    body: AssessmentIn,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Record a self-assessment of the practices the data can't see. Unanswered
    questions count as "no"."""
    try:
        out = await maturity.save_assessment(db, tenant_id, body.answers, created_by=user.email)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    await db.commit()
    return out
