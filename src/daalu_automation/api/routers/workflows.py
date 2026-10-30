from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id
from daalu_automation.api.schemas import WorkflowDescriptorOut, WorkflowRunOut
from daalu_automation.core.workflows import list_workflows, run_workflow
from daalu_automation.database import get_db
from daalu_automation.models import Alert, WorkflowRun

router = APIRouter(prefix="/workflows", tags=["workflows"])


class RunWorkflowRequest(BaseModel):
    name: str
    input: dict = {}


async def _to_out(
    db: AsyncSession, runs: list[WorkflowRun]
) -> list[WorkflowRunOut]:
    """Serialize runs, batch-resolving each linked alert's title."""
    alert_ids = {r.alert_id for r in runs if r.alert_id}
    titles: dict[uuid.UUID, str] = {}
    if alert_ids:
        rows = (
            await db.execute(
                select(Alert.id, Alert.title).where(Alert.id.in_(alert_ids))
            )
        ).all()
        titles = {aid: title for aid, title in rows}
    out: list[WorkflowRunOut] = []
    for r in runs:
        o = WorkflowRunOut.model_validate(r)
        o.alert_title = titles.get(r.alert_id) if r.alert_id else None
        out.append(o)
    return out


@router.get("", response_model=list[WorkflowDescriptorOut])
async def list_workflows_route():
    return [
        WorkflowDescriptorOut(name=name, module=module) for name, module in list_workflows()
    ]


@router.get("/runs", response_model=list[WorkflowRunOut])
async def list_workflow_runs(
    module: str | None = Query(default=None),
    # "alert" → only agent remediation runs (those tied to an alert), which is
    # what the Workflows page shows.
    source: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    stmt = (
        select(WorkflowRun)
        .where(WorkflowRun.tenant_id == tenant_id)
        .order_by(desc(WorkflowRun.created_at))
        .limit(limit)
    )
    if module:
        stmt = stmt.where(WorkflowRun.module == module)
    if source == "alert":
        stmt = stmt.where(WorkflowRun.alert_id.is_not(None))
    runs = list((await db.execute(stmt)).scalars().all())
    return await _to_out(db, runs)


@router.get("/runs/{run_id}", response_model=WorkflowRunOut)
async def get_workflow_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    run = (
        await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "workflow run not found")
    return (await _to_out(db, [run]))[0]


@router.post("/run", status_code=202)
async def run_workflow_route(
    req: RunWorkflowRequest, tenant_id=Depends(current_tenant_id)
):
    # The caller's tenant, never one from the request body: workflows (e.g.
    # infra.incident.coordinate) read tenant_id from their input, and trusting
    # the client's let one tenant open or update another's incidents.
    payload = {**(req.input or {}), "tenant_id": str(tenant_id)}
    run_id = await run_workflow(req.name, payload, tenant_id=tenant_id)
    return {"run_id": str(run_id)}
