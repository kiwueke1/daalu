"""Hash-chained audit log: list + verify. docs/design/ai-ops-maturity.md §2."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id
from daalu_automation.core.aiops import audit
from daalu_automation.database import get_db
from daalu_automation.models import AuditEvent

router = APIRouter(prefix="/aiops", tags=["aiops"])


class AuditEventOut(BaseModel):
    id: uuid.UUID
    seq: int
    kind: str
    actor: str
    subject: str
    data: dict[str, Any]
    prev_hash: str
    hash: str
    created_at: datetime


@router.get("/audit", response_model=list[AuditEventOut])
async def list_audit(
    kind: str | None = None,
    subject: str | None = None,
    actor: str | None = None,
    before_seq: int | None = None,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    q = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
    if kind:
        q = q.where(AuditEvent.kind.like(kind.replace("*", "%")))
    if subject:
        q = q.where(AuditEvent.subject == subject)
    if actor:
        q = q.where(AuditEvent.actor == actor)
    if before_seq:
        q = q.where(AuditEvent.seq < before_seq)
    q = q.order_by(AuditEvent.seq.desc()).limit(max(1, min(limit, 1000)))
    rows = (await db.execute(q)).scalars().all()
    return [
        AuditEventOut(
            id=r.id, seq=r.seq, kind=r.kind, actor=r.actor, subject=r.subject,
            data=r.data or {}, prev_hash=r.prev_hash, hash=r.hash, created_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/audit/verify")
async def verify_audit(
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
) -> dict[str, Any]:
    return await audit.verify_chain(db, tenant_id)


@router.get("/audit/kinds")
async def audit_kinds() -> list[str]:
    return list(audit.KINDS)
