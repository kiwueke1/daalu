"""Append-only, hash-chained audit log of everything the AI and its approvers do.

Each tenant has its own chain. Event ``n`` stores ``prev_hash`` (event
``n-1``'s hash) and ``hash = sha256(prev_hash + canonical_json(body))``. Editing
or deleting any row breaks every hash after it, which :func:`verify_chain`
reports. Writers serialise per tenant with a transaction-scoped Postgres
advisory lock, so concurrent writers can't fork the chain.

``record`` never raises into the caller's business logic: an audit write
failing must not turn a successful remediation into a 500. It logs instead.
The caller's transaction commits the event together with the change it
describes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

KINDS = (
    "tool.read",
    "action.proposed",
    "action.denied",
    "action.approved",
    "action.approval_recorded",
    "action.rejected",
    "action.executed",
    "action.auto_executed",
    "action.verified",
    "policy.updated",
    "policy.kill_switch",
    "policy.breaker_tripped",
    "investigation.verdict",
    "runbook.published",
    "runbook.drafted",
    "mcp.call",
    "eval.recorded",
)


def _canonical(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, body: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + _canonical(body)).encode()).hexdigest()


def _body(seq: int, kind: str, actor: str, subject: str, data: dict, created_at: datetime) -> dict:
    return {
        "seq": seq,
        "kind": kind,
        "actor": actor,
        "subject": subject,
        "data": data,
        "created_at": created_at.astimezone(timezone.utc).isoformat(),
    }


async def record(
    db: AsyncSession,
    tenant_id: uuid.UUID | str,
    kind: str,
    *,
    actor: str = "",
    subject: str = "",
    data: dict[str, Any] | None = None,
) -> Any | None:
    from daalu_automation.models import AuditEvent

    tid = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    payload = json.loads(_canonical(data or {}))  # JSON-safe copy
    try:
        async with db.begin_nested():
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                {"k": f"audit:{tid}"},
            )
            last = (
                await db.execute(
                    select(AuditEvent.seq, AuditEvent.hash)
                    .where(AuditEvent.tenant_id == tid)
                    .order_by(AuditEvent.seq.desc())
                    .limit(1)
                )
            ).first()
            seq = (last[0] + 1) if last else 1
            prev = last[1] if last else ""
            now = datetime.now(tz=timezone.utc)
            ev = AuditEvent(
                tenant_id=tid,
                seq=seq,
                kind=kind,
                actor=actor[:255],
                subject=str(subject)[:255],
                data=payload,
                prev_hash=prev,
                hash=compute_hash(prev, _body(seq, kind, actor[:255], str(subject)[:255], payload, now)),
                created_at=now,
                updated_at=now,
            )
            db.add(ev)
            await db.flush()
        return ev
    except Exception:  # noqa: BLE001
        logger.exception("aiops.audit.record_failed", kind=kind, tenant_id=str(tid))
        return None


async def verify_chain(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    """Walk the tenant's chain. Returns ``{ok, checked, broken_at, reason}``."""
    from daalu_automation.models import AuditEvent

    rows = (
        await db.execute(
            select(AuditEvent)
            .where(AuditEvent.tenant_id == tenant_id)
            .order_by(AuditEvent.seq.asc())
        )
    ).scalars().all()
    prev = ""
    expected_seq = 1
    for ev in rows:
        if ev.seq != expected_seq:
            return {"ok": False, "checked": expected_seq - 1, "broken_at": ev.seq,
                    "reason": f"sequence gap: expected {expected_seq}, found {ev.seq}"}
        if ev.prev_hash != prev:
            return {"ok": False, "checked": expected_seq - 1, "broken_at": ev.seq,
                    "reason": "prev_hash does not match the previous event"}
        want = compute_hash(prev, _body(ev.seq, ev.kind, ev.actor, ev.subject, ev.data, ev.created_at))
        if want != ev.hash:
            return {"ok": False, "checked": expected_seq - 1, "broken_at": ev.seq,
                    "reason": "event content does not match its hash (edited)"}
        prev = ev.hash
        expected_seq += 1
    return {"ok": True, "checked": len(rows), "broken_at": None, "reason": ""}
