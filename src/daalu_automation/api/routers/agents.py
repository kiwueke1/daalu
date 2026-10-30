from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id
from daalu_automation.api.schemas import AgentDescriptorOut, AgentRunOut
from daalu_automation.core.agents import list_agents
from daalu_automation.database import get_db
from daalu_automation.models import AgentRun

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[AgentDescriptorOut])
async def list_agents_route():
    return [
        AgentDescriptorOut(
            name=d.name,
            module=d.module,
            description=d.description,
            subscribed_event_types=list(d.subscribed_event_types),
        )
        for d in list_agents()
    ]


@router.get("/runs", response_model=list[AgentRunOut])
async def list_agent_runs(
    agent_name: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    stmt = (
        select(AgentRun)
        .where(AgentRun.tenant_id == tenant_id)
        .order_by(desc(AgentRun.started_at))
        .limit(limit)
    )
    if agent_name:
        stmt = stmt.where(AgentRun.agent_name == agent_name)
    return (await db.execute(stmt)).scalars().all()
