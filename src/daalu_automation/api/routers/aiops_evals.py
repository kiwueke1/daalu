"""Eval harness: scenarios, recorded runs, per-model leaderboard.
docs/design/ai-ops-maturity.md §8, §13."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id, current_user
from daalu_automation.core.aiops import audit
from daalu_automation.database import get_db
from daalu_automation.evals.grader import PASS_THRESHOLD
from daalu_automation.evals.scenario import ScenarioError, load_scenarios
from daalu_automation.evals.scoreboard import leaderboard
from daalu_automation.models import Alert, EvalRun, User

router = APIRouter(prefix="/aiops", tags=["aiops"])


class EvalRunIn(BaseModel):
    scenario_id: str = Field(min_length=1, max_length=128)
    label: str = Field(default="", max_length=128)
    alert_id: uuid.UUID | None = None
    started_at: datetime
    finished_at: datetime | None = None
    score: float = Field(ge=0.0, le=1.0)
    passed: bool | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class EvalRunOut(BaseModel):
    id: uuid.UUID
    scenario_id: str
    label: str
    alert_id: uuid.UUID | None
    started_at: datetime
    finished_at: datetime | None
    score: float
    passed: bool
    details: dict[str, Any]


def _out(r: EvalRun) -> dict[str, Any]:
    return {
        "id": r.id, "scenario_id": r.scenario_id, "label": r.label, "alert_id": r.alert_id,
        "started_at": r.started_at, "finished_at": r.finished_at, "score": r.score,
        "passed": r.passed, "details": r.details or {},
    }


@router.get("/evals/scenarios")
async def list_scenarios(_tenant_id=Depends(current_tenant_id)) -> list[dict[str, Any]]:
    try:
        return [s.summary() for s in load_scenarios()]
    except ScenarioError as e:
        raise HTTPException(500, f"invalid scenario: {e}") from e


@router.get("/evals/runs", response_model=list[EvalRunOut])
async def list_runs(
    scenario_id: str | None = None,
    label: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    q = select(EvalRun).where(EvalRun.tenant_id == tenant_id)
    if scenario_id:
        q = q.where(EvalRun.scenario_id == scenario_id)
    if label is not None and label != "":
        q = q.where(EvalRun.label == label)
    q = q.order_by(EvalRun.started_at.desc()).limit(limit)
    return [_out(r) for r in (await db.execute(q)).scalars().all()]


@router.get("/evals/leaderboard")
async def get_leaderboard(
    days: int | None = Query(default=None, ge=1, le=3650),
    scenario_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> list[dict[str, Any]]:
    """``[{label, runs, mean_score, pass_rate, scenarios, per_scenario,
    median_time_to_triage_s}]``, best first. Runs flagged invalid (answered
    by the wrong model) are excluded."""
    q = select(EvalRun).where(EvalRun.tenant_id == tenant_id)
    if days:
        q = q.where(EvalRun.started_at >= datetime.now(tz=timezone.utc) - timedelta(days=days))
    if scenario_id:
        q = q.where(EvalRun.scenario_id == scenario_id)
    return leaderboard((await db.execute(q)).scalars().all())


@router.post("/evals/runs", response_model=EvalRunOut)
async def record_run(
    body: EvalRunIn,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    if body.alert_id is not None:
        owned = (
            await db.execute(
                select(Alert.id).where(Alert.id == body.alert_id, Alert.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if owned is None:
            raise HTTPException(422, "alert_id does not belong to this tenant")
    passed = body.passed if body.passed is not None else body.score >= PASS_THRESHOLD
    run = EvalRun(
        tenant_id=tenant_id, scenario_id=body.scenario_id, label=body.label,
        alert_id=body.alert_id, started_at=body.started_at,
        finished_at=body.finished_at or datetime.now(tz=timezone.utc),
        score=body.score, passed=passed, details=body.details,
    )
    db.add(run)
    await db.flush()
    await audit.record(
        db, tenant_id, "eval.recorded", actor=user.email,
        subject=str(body.alert_id or body.scenario_id),
        data={"eval_run_id": str(run.id), "scenario_id": body.scenario_id,
              "label": body.label, "score": body.score, "passed": passed},
    )
    await db.commit()
    await db.refresh(run)
    return _out(run)
