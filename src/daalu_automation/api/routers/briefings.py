"""Briefings CRUD + on-demand generation."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id
from daalu_automation.api.schemas import BriefingOut
from daalu_automation.core.briefings import get_briefing_generator, list_briefings
from daalu_automation.database import get_db
from daalu_automation.models import Briefing, BriefingChannel

router = APIRouter(prefix="/briefings", tags=["briefings"])


@router.get("/channels", response_model=list[str])
async def list_channels():
    return [c.value for c in list_briefings()]


@router.get("", response_model=list[BriefingOut])
async def list_briefings_route(
    channel: str | None = Query(default=None),
    limit: int = Query(default=30, le=100),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    stmt = (
        select(Briefing)
        .where(Briefing.tenant_id == tenant_id)
        .order_by(desc(Briefing.coverage_date), desc(Briefing.created_at))
        .limit(limit)
    )
    if channel:
        try:
            stmt = stmt.where(Briefing.channel == BriefingChannel(channel))
        except ValueError as e:
            raise HTTPException(400, f"unknown channel: {channel}") from e
    return (await db.execute(stmt)).scalars().all()


@router.get("/latest", response_model=BriefingOut)
async def latest_briefing(
    channel: str = Query(...),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    try:
        channel_enum = BriefingChannel(channel)
    except ValueError as e:
        raise HTTPException(400, f"unknown channel: {channel}") from e
    row = (
        await db.execute(
            select(Briefing)
            .where(Briefing.tenant_id == tenant_id, Briefing.channel == channel_enum)
            .order_by(desc(Briefing.coverage_date), desc(Briefing.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"no briefing yet for channel={channel}")
    return row


@router.get("/{briefing_id}", response_model=BriefingOut)
async def get_briefing(
    briefing_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    row = (
        await db.execute(
            select(Briefing).where(
                Briefing.id == briefing_id, Briefing.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"briefing {briefing_id} not found")
    return row


@router.post("/{channel}/generate", response_model=BriefingOut, status_code=202)
async def generate_briefing_route(
    channel: str,
    background: BackgroundTasks,
    tenant_id=Depends(current_tenant_id),
):
    try:
        channel_enum = BriefingChannel(channel)
    except ValueError as e:
        raise HTTPException(400, f"unknown channel: {channel}") from e
    generator = get_briefing_generator(channel_enum)
    # We do this inline (await) so the caller gets the freshly generated
    # row in the response. For prod the celery beat schedule handles
    # bulk generation; this endpoint is for ad-hoc "generate now" actions.
    briefing = await generator.generate(tenant_id=tenant_id)
    return briefing
