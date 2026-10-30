"""ChangeProposal HTTP routes — list / get / approve / reject.

There is intentionally **no** ``/execute`` route here. Execution is the
executor service's job: it talks to the DB + adapter directly with an
executor-scoped JWT (see :func:`core.auth.mint_executor_token`). PR 2
will land the executor microservice; for PR 1 the integration test
calls :func:`core.change_proposals.execute` from inside the test
process with a hand-built :class:`Actor`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id, current_user
from daalu_automation.api.schemas import ChangeProposalOut
from daalu_automation.core import change_proposals as cps
from daalu_automation.core.sot.models import Actor
from daalu_automation.database import get_db
from daalu_automation.models import (
    ChangeProposal,
    ChangeProposalKind,
    ChangeProposalStatus,
    User,
)

router = APIRouter(prefix="/change-proposals", tags=["change_proposals"])


@router.get("", response_model=list[ChangeProposalOut])
async def list_change_proposals(
    status: str | None = Query(default=None),
    device_id: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    stmt = (
        select(ChangeProposal)
        .where(ChangeProposal.tenant_id == tenant_id)
        .order_by(desc(ChangeProposal.created_at))
        .limit(limit)
    )
    if status:
        stmt = stmt.where(ChangeProposal.status == status)
    if device_id:
        stmt = stmt.where(ChangeProposal.device_id == device_id)
    if kind:
        stmt = stmt.where(ChangeProposal.kind == kind)
    return (await db.execute(stmt)).scalars().all()


async def _get_for_tenant(
    db: AsyncSession, tenant_id, proposal_id: str
) -> ChangeProposal:
    """Fetch a proposal scoped to the caller's tenant.

    Returns 404 (not 403) on tenant mismatch — same convention as the
    other routers (alerts.py:_get_tenant_alert).
    """
    try:
        pid = uuid.UUID(proposal_id)
    except ValueError as e:
        raise HTTPException(404, "change proposal not found") from e
    stmt = select(ChangeProposal).where(
        ChangeProposal.id == pid,
        ChangeProposal.tenant_id == tenant_id,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "change proposal not found")
    return row


@router.get("/{proposal_id}", response_model=ChangeProposalOut)
async def get_change_proposal(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    return await _get_for_tenant(db, tenant_id, proposal_id)


@router.post("/{proposal_id}/approve", response_model=ChangeProposalOut)
async def approve_change_proposal(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    # Verify ownership first so cross-tenant probing returns 404 before
    # we touch the service layer.
    row = await _get_for_tenant(db, tenant_id, proposal_id)
    actor = Actor(kind="user", user_id=user.id)
    try:
        return await cps.approve(db, tenant_id, row.id, actor=actor)
    except cps.ProposalStatusError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/{proposal_id}/reject", response_model=ChangeProposalOut)
async def reject_change_proposal(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    row = await _get_for_tenant(db, tenant_id, proposal_id)
    actor = Actor(kind="user", user_id=user.id)
    try:
        return await cps.reject(db, tenant_id, row.id, actor=actor)
    except cps.ProposalStatusError as e:
        raise HTTPException(409, str(e)) from e


# Re-export enums for callers that want their values without an extra
# import — matches the pattern used elsewhere in the router layer.
__all__ = ["ChangeProposalKind", "ChangeProposalStatus", "router"]
