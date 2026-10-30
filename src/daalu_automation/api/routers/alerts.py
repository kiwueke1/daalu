from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id
from daalu_automation.api.schemas import AlertOccurrenceOut, AlertOut
from daalu_automation.database import get_db
from daalu_automation.models import Alert, AlertOccurrence, AlertStatus

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    module: str | None = Query(default=None),
    status: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    # Sort by recency of the most recent fire, not just the original
    # creation time — when a stale alert re-fires we want it to bubble
    # back to the top of the tile list.
    stmt = (
        select(Alert)
        .where(Alert.tenant_id == tenant_id)
        .order_by(desc(func.coalesce(Alert.last_seen_at, Alert.created_at)))
        .limit(limit)
    )
    if module:
        stmt = stmt.where(Alert.module == module)
    if status:
        stmt = stmt.where(Alert.status == status)
    if severity:
        stmt = stmt.where(Alert.severity == severity)
    return (await db.execute(stmt)).scalars().all()


async def _get_tenant_alert(
    db: AsyncSession, alert_id: str, tenant_id
) -> Alert:
    """Fetch an alert scoped to the caller's tenant.

    Returns 404 (not 403) on tenant mismatch — leaking existence-by-UUID
    would let one tenant probe another's row IDs.
    """
    stmt = select(Alert).where(Alert.id == alert_id, Alert.tenant_id == tenant_id)
    alert = (await db.execute(stmt)).scalar_one_or_none()
    if alert is None:
        raise HTTPException(404, "alert not found")
    return alert


@router.get("/{alert_id}", response_model=AlertOut)
async def get_alert(
    alert_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    return await _get_tenant_alert(db, alert_id, tenant_id)


@router.get(
    "/{alert_id}/occurrences",
    response_model=list[AlertOccurrenceOut],
)
async def list_alert_occurrences(
    alert_id: str,
    limit: int = Query(default=200, le=1000),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    """Return the fire-time history for this alert, newest first.

    Each row is one time the underlying signal fired. The detail page
    uses this to draw the "fired N times at …" timeline.
    """
    # 404 the alert lookup first so cross-tenant probing returns the
    # same shape as :func:`get_alert`.
    await _get_tenant_alert(db, alert_id, tenant_id)
    stmt = (
        select(AlertOccurrence)
        .where(
            AlertOccurrence.alert_id == alert_id,
            AlertOccurrence.tenant_id == tenant_id,
        )
        .order_by(desc(AlertOccurrence.occurred_at))
        .limit(limit)
    )
    return (await db.execute(stmt)).scalars().all()


@router.post("/{alert_id}/acknowledge", response_model=AlertOut)
async def acknowledge_alert(
    alert_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    alert = await _get_tenant_alert(db, alert_id, tenant_id)
    alert.status = AlertStatus.acknowledged
    alert.acknowledged_at = datetime.now(tz=timezone.utc)
    await db.commit()
    await db.refresh(alert)
    return alert


@router.post("/{alert_id}/resolve", response_model=AlertOut)
async def resolve_alert(
    alert_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    alert = await _get_tenant_alert(db, alert_id, tenant_id)
    alert.status = AlertStatus.resolved
    alert.resolved_at = datetime.now(tz=timezone.utc)
    await db.commit()
    await db.refresh(alert)
    return alert
