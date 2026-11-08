"""Investigations (learning-loop records) and human verdicts.
docs/design/ai-ops-maturity.md §4."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id, current_user
from daalu_automation.core.aiops import audit
from daalu_automation.core.aiops import investigations as inv_mod
from daalu_automation.database import get_db
from daalu_automation.models import Alert, AlertAction, Investigation, User

router = APIRouter(prefix="/aiops", tags=["aiops"])


class InvestigationOut(BaseModel):
    id: uuid.UUID
    alert_id: uuid.UUID
    alert_title: str
    alert_severity: str
    alert_status: str
    alert_created_at: datetime
    started_at: datetime
    triaged_at: datetime | None
    time_to_triage_s: float | None
    root_cause: str
    actions_proposed: int
    actions_approved: int
    actions_rejected: int
    actions_auto: int
    resolution: str
    resolved_at: datetime | None
    time_to_resolve_s: float | None
    verified: bool | None
    runbook_ids: list[str]
    verdict: str | None
    verdict_by: str
    verdict_at: datetime | None
    verdict_note: str
    model: str


class InvestigationDetail(InvestigationOut):
    actions: list[dict[str, Any]]


class VerdictIn(BaseModel):
    verdict: Literal["correct", "partial", "wrong"]
    note: str = Field(default="", max_length=4000)


def _secs(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return max(0.0, (a - b).total_seconds())


def _out(inv: Investigation, alert: Alert) -> dict[str, Any]:
    return {
        "id": inv.id,
        "alert_id": inv.alert_id,
        "alert_title": alert.title,
        "alert_severity": alert.severity.value,
        "alert_status": alert.status.value,
        "alert_created_at": alert.created_at,
        "started_at": inv.started_at,
        "triaged_at": inv.triaged_at,
        "time_to_triage_s": _secs(inv.triaged_at, alert.created_at),
        "root_cause": inv.root_cause,
        "actions_proposed": inv.actions_proposed,
        "actions_approved": inv.actions_approved,
        "actions_rejected": inv.actions_rejected,
        "actions_auto": inv.actions_auto,
        "resolution": inv.resolution,
        "resolved_at": inv.resolved_at,
        "time_to_resolve_s": _secs(inv.resolved_at, alert.created_at),
        "verified": inv.verified,
        "runbook_ids": [str(x) for x in (inv.runbook_ids or [])],
        "verdict": inv.verdict,
        "verdict_by": inv.verdict_by,
        "verdict_at": inv.verdict_at,
        "verdict_note": inv.verdict_note,
        "model": inv.model,
    }


@router.get("/investigations", response_model=list[InvestigationOut])
async def list_investigations(
    days: int = 30,
    resolution: str | None = None,
    needs_verdict: bool = False,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    since = datetime.now(tz=timezone.utc) - timedelta(days=max(1, min(days, 365)))
    q = (
        select(Investigation, Alert)
        .join(Alert, Alert.id == Investigation.alert_id)
        .where(Investigation.tenant_id == tenant_id, Investigation.started_at >= since)
    )
    if resolution:
        q = q.where(Investigation.resolution == resolution)
    if needs_verdict:
        q = q.where(Investigation.verdict.is_(None), Investigation.triaged_at.is_not(None))
    q = q.order_by(Investigation.started_at.desc()).limit(max(1, min(limit, 1000)))
    rows = (await db.execute(q)).all()
    return [_out(i, a) for i, a in rows]


async def _get(db: AsyncSession, tenant_id, inv_id: str) -> tuple[Investigation, Alert]:
    try:
        iid = uuid.UUID(inv_id)
    except ValueError as e:
        raise HTTPException(404, "investigation not found") from e
    row = (
        await db.execute(
            select(Investigation, Alert)
            .join(Alert, Alert.id == Investigation.alert_id)
            .where(Investigation.id == iid, Investigation.tenant_id == tenant_id)
        )
    ).first()
    if row is None:
        raise HTTPException(404, "investigation not found")
    return row[0], row[1]


@router.get("/investigations/by-alert/{alert_id}", response_model=InvestigationDetail)
async def get_by_alert(
    alert_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    try:
        aid = uuid.UUID(alert_id)
    except ValueError as e:
        raise HTTPException(404, "investigation not found") from e
    inv = (
        await db.execute(
            select(Investigation).where(
                Investigation.alert_id == aid, Investigation.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if inv is None:
        raise HTTPException(404, "investigation not found")
    return await get_investigation(str(inv.id), db=db, tenant_id=tenant_id)


@router.get("/investigations/{inv_id}", response_model=InvestigationDetail)
async def get_investigation(
    inv_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    inv, alert = await _get(db, tenant_id, inv_id)
    actions = (
        await db.execute(
            select(AlertAction)
            .where(AlertAction.alert_id == alert.id, AlertAction.requires_approval.is_(True))
            .order_by(AlertAction.created_at.asc())
        )
    ).scalars().all()
    return {
        **_out(inv, alert),
        "actions": [
            {
                "id": str(a.id), "tool": a.tool_name, "input": a.tool_input,
                "status": a.status.value, "policy_mode": a.policy_mode,
                "policy_rule": a.policy_rule, "auto_executed": a.auto_executed,
                "verified_resolved": a.verified_resolved,
                "approvals": a.approvals or [], "actor": a.actor,
            }
            for a in actions
        ],
    }


@router.post("/investigations/{inv_id}/verdict", response_model=InvestigationOut)
async def submit_verdict(
    inv_id: str,
    body: VerdictIn,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    """Grade the AI's root cause: correct / partial / wrong. This is the
    ground truth behind the agreement rate and the eval material."""
    inv, alert = await _get(db, tenant_id, inv_id)
    inv_mod.set_verdict(inv, body.verdict, by=user.email, note=body.note)
    await audit.record(
        db, tenant_id, "investigation.verdict", actor=user.email, subject=str(alert.id),
        data={"investigation_id": str(inv.id), "verdict": body.verdict, "note": body.note},
    )
    await db.commit()
    return _out(inv, alert)
