"""Infra-specific REST surface."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id
from daalu_automation.api.schemas import IncidentFromAlertRequest, IncidentOut
from daalu_automation.database import get_db
from daalu_automation.models import Alert, Incident
from daalu_automation.models.infra import IncidentSeverity, IncidentStatus

router = APIRouter(prefix="/infra", tags=["infra"])

# Alert severities don't 1:1 map to incident severities — alerts are
# info/warning/critical, incidents are sev1..sev4. Pick the closest
# escalation when the caller doesn't override.
_ALERT_TO_INCIDENT_SEVERITY = {
    "critical": IncidentSeverity.sev2,
    "warning": IncidentSeverity.sev3,
    "info": IncidentSeverity.sev4,
}


@router.get("/incidents", response_model=list[IncidentOut])
async def list_incidents(
    status: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    stmt = (
        select(Incident)
        .where(Incident.tenant_id == tenant_id)
        .order_by(desc(Incident.started_at))
        .limit(limit)
    )
    if status:
        stmt = stmt.where(Incident.status == status)
    if severity:
        stmt = stmt.where(Incident.severity == severity)
    return (await db.execute(stmt)).scalars().all()


@router.get("/incidents/{incident_id}", response_model=IncidentOut)
async def get_incident(
    incident_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    stmt = select(Incident).where(
        Incident.id == incident_id, Incident.tenant_id == tenant_id
    )
    incident = (await db.execute(stmt)).scalar_one_or_none()
    if incident is None:
        raise HTTPException(404, "incident not found")
    return incident


@router.post(
    "/incidents/from-alert/{alert_id}",
    response_model=IncidentOut,
    status_code=201,
)
async def promote_alert_to_incident(
    alert_id: str,
    payload: IncidentFromAlertRequest,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    """Promote a single Alert into a new Incident.

    Source-alert reference is stored in ``incident.evidence`` so the
    incident page can backlink without a join table.
    """
    alert_stmt = select(Alert).where(
        Alert.id == alert_id, Alert.tenant_id == tenant_id
    )
    alert = (await db.execute(alert_stmt)).scalar_one_or_none()
    if alert is None:
        raise HTTPException(404, "alert not found")

    if payload.severity:
        try:
            severity = IncidentSeverity(payload.severity)
        except ValueError as exc:
            raise HTTPException(400, "invalid severity") from exc
    else:
        severity = _ALERT_TO_INCIDENT_SEVERITY.get(
            alert.severity.value, IncidentSeverity.sev3
        )

    incident = Incident(
        tenant_id=tenant_id,
        title=payload.title.strip() or alert.title,
        summary=(payload.summary or "").strip(),
        severity=severity,
        status=IncidentStatus.open,
        started_at=alert.created_at or datetime.now(tz=timezone.utc),
        evidence=[
            {
                "type": "alert",
                "alert_id": str(alert.id),
                "title": alert.title,
                "severity": alert.severity.value,
                "module": alert.module,
            }
        ],
    )
    db.add(incident)
    await db.commit()
    await db.refresh(incident)
    return incident
