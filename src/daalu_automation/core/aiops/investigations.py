"""Investigations: the learning-loop record for each AI investigation (AOMM L4).

One row per alert. The alert chat loop keeps it current:

* :func:`ensure` when the agent first answers,
* :func:`note_answer` on every assistant message (sets ``triaged_at`` and
  ``root_cause`` from the first ``## Root cause`` section),
* :func:`bump` as actions are proposed / approved / rejected / auto-run,
* :func:`note_outcome` after verification.

Humans then grade it with a verdict, which is what turns the record into
the agreement rate and eval material.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

VERDICTS = ("correct", "partial", "wrong")

_ROOT_CAUSE_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?root cause(?:\*\*)?[ \t]*:?[ \t]*\n?(?P<body>.*?)"
    r"(?=^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?(?:background|remediation plan)\b|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def extract_root_cause(text: str) -> str:
    m = _ROOT_CAUSE_RE.search(text or "")
    if not m:
        return ""
    return m.group("body").strip()[:4000]


async def get_for_alert(db: AsyncSession, alert_id: uuid.UUID) -> Any | None:
    from daalu_automation.models import Investigation

    return (
        await db.execute(select(Investigation).where(Investigation.alert_id == alert_id))
    ).scalar_one_or_none()


async def ensure(db: AsyncSession, *, tenant_id: uuid.UUID, alert_id: uuid.UUID) -> Any:
    from daalu_automation.models import Investigation

    inv = await get_for_alert(db, alert_id)
    if inv is None:
        inv = Investigation(
            tenant_id=tenant_id,
            alert_id=alert_id,
            started_at=datetime.now(tz=timezone.utc),
            runbook_ids=[],
        )
        db.add(inv)
        await db.flush()
    return inv


def note_answer(inv: Any, text: str, *, model: str = "") -> None:
    if model and not inv.model:
        inv.model = model[:128]
    if inv.triaged_at is not None:
        return
    rc = extract_root_cause(text)
    if rc:
        inv.root_cause = rc
        inv.triaged_at = datetime.now(tz=timezone.utc)


def bump(inv: Any, field: str, by: int = 1) -> None:
    setattr(inv, field, int(getattr(inv, field) or 0) + by)


def note_outcome(inv: Any, *, resolved: bool, auto: bool) -> None:
    inv.verified = resolved
    if resolved:
        inv.resolution = "auto" if auto else "approved"
        inv.resolved_at = datetime.now(tz=timezone.utc)
    elif inv.resolution == "open":
        inv.resolution = "unresolved"


def set_verdict(inv: Any, verdict: str, *, by: str, note: str = "") -> None:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    inv.verdict = verdict
    inv.verdict_by = by[:255]
    inv.verdict_note = note[:4000]
    inv.verdict_at = datetime.now(tz=timezone.utc)
