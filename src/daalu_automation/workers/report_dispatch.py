"""Cron-driven dispatcher for ReportSchedule rows.

Beat fires :func:`dispatch_due_schedules` every 60 s. Any schedule whose
``next_run_at`` is in the past and which is ``enabled`` gets:

1. Its SavedReport.definition executed via the same path /reports/query
   uses (so filters + entity whitelisting are identical).
2. The result formatted (markdown table or CSV).
3. Delivered through ``core.notify.send_slack`` / ``send_email``.
4. ``last_run_at`` + ``last_status`` updated, ``next_run_at`` advanced.

Errors are recorded on the row (``last_status='failed'``,
``last_error=...``) but never bubble up to fail the whole pass — one
broken schedule should not starve the others.
"""

from __future__ import annotations

import asyncio
import csv
import io
import traceback
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.routers.reports import QueryRequest, _execute_query
from daalu_automation.core.notify import send_email, send_slack
from daalu_automation.database import AsyncSessionLocal, engine
from daalu_automation.models import ReportSchedule, SavedReport
from daalu_automation.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)


# ── Minimal 5-field cron evaluator ──────────────────────────────────────
#
# We need "given expression X and instant T, what's the next T' > T at which
# X matches?" — for sub-minute precision on user-defined schedules.
#
# Celery's :class:`crontab.remaining_estimate` is meant for beat-scheduler
# next-wakeup estimation, not exact next-fire computation, so it can return
# times the actual cron doesn't match. We do our own field expansion
# instead — small enough to be obvious, exact enough to trust.
#
# Field semantics (same as cron / celery):
#   minute       0-59
#   hour         0-23
#   day-of-month 1-31
#   month        1-12
#   day-of-week  0-6  (0=Sun, 6=Sat; legacy 7 is folded to 0)


def _parse_field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s), int(b_s)
        else:
            v = int(part)
            a = b = v
        for v in range(a, b + 1, step):
            if lo <= v <= hi:
                out.add(v)
    return out


def _parse_cron_fields(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron must have 5 fields, got {expr!r}")
    minute = _parse_field(parts[0], 0, 59)
    hour = _parse_field(parts[1], 0, 23)
    dom = _parse_field(parts[2], 1, 31)
    month = _parse_field(parts[3], 1, 12)
    dow_raw = _parse_field(parts[4], 0, 7)
    # Fold legacy 7 (Sun in some cron variants) into 0.
    dow = {0 if v == 7 else v for v in dow_raw}
    return minute, hour, dom, month, dow


def _matches(dt: datetime, fields: tuple[set[int], set[int], set[int], set[int], set[int]]) -> bool:
    minute, hour, dom, month, dow = fields
    # Python weekday: Mon=0..Sun=6. Cron: Sun=0..Sat=6.
    py_dow = (dt.weekday() + 1) % 7
    return (
        dt.minute in minute
        and dt.hour in hour
        and dt.day in dom
        and dt.month in month
        and py_dow in dow
    )


def compute_next_run(cron_expr: str, *, after: datetime | None = None) -> datetime:
    """Return the next minute-aligned datetime ``> after`` at which
    ``cron_expr`` matches. Searches up to ~370 days forward; raises
    ``ValueError`` if no match.
    """
    fields = _parse_cron_fields(cron_expr)
    base = (after or datetime.now(tz=timezone.utc)).replace(second=0, microsecond=0)
    base += timedelta(minutes=1)
    # 370 days bounds the worst-case search for "yearly" exprs like "0 0 29 2 *".
    for _ in range(60 * 24 * 370):
        if _matches(base, fields):
            return base
        base += timedelta(minutes=1)
    raise ValueError(f"no next fire time within a year for {cron_expr!r}")


def _format_markdown(name: str, result) -> str:
    """Build a small markdown summary suitable for Slack / email body."""
    cols = result.columns
    rows = result.rows[:25]
    lines = [f"*{name}* — {result.total} {result.entity}"]
    if result.display == "count":
        lines.append(f"\n**{result.total}** matching")
        return "\n".join(lines)
    if not rows:
        lines.append("\n_no rows_")
        return "\n".join(lines)
    lines.append("")
    lines.append("| " + " | ".join(c.label for c in cols) + " |")
    lines.append("|" + "|".join("---" for _ in cols) + "|")
    for r in rows:
        cells = [_short(r.get(c.key)) for c in cols]
        lines.append("| " + " | ".join(cells) + " |")
    if result.total > len(rows):
        lines.append(f"\n_+{result.total - len(rows)} more rows_")
    return "\n".join(lines)


def _format_csv(result) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c.label for c in result.columns])
    for r in result.rows:
        w.writerow([_short(r.get(c.key)) for c in result.columns])
    return buf.getvalue()


def _short(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return "…"
    s = str(v)
    return s if len(s) <= 80 else s[:77] + "…"


async def _dispatch_one(db: AsyncSession, sched: ReportSchedule) -> None:
    saved = (
        await db.execute(
            select(SavedReport).where(
                SavedReport.id == sched.saved_report_id,
                SavedReport.tenant_id == sched.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if saved is None:
        sched.last_status = "failed"
        sched.last_error = "saved report no longer exists"
        return

    req = QueryRequest(**saved.definition)
    result = await _execute_query(req, db, sched.tenant_id)

    body_md = _format_markdown(saved.name, result)
    if sched.destination == "slack":
        delivered = await send_slack(
            body_md,
            tenant_id=sched.tenant_id,
            db=db,
            channel=sched.recipient or None,
        )
    elif sched.destination == "email":
        if not sched.recipient:
            raise RuntimeError("email schedule has no recipient")
        subject = f"[Daalu] {saved.name}"
        body = body_md if sched.fmt == "markdown" else _format_csv(result)
        delivered = await send_email(
            to=sched.recipient,
            subject=subject,
            body_markdown=body,
            tenant_id=sched.tenant_id,
            db=db,
        )
    else:
        raise RuntimeError(f"unknown destination: {sched.destination}")

    if not delivered:
        sched.last_status = "failed"
        sched.last_error = "delivery unconfigured or refused"
        return
    sched.last_status = "ok"
    sched.last_error = None


async def _dispatch_due_async() -> int:
    now = datetime.now(tz=timezone.utc)
    fired = 0
    async with AsyncSessionLocal() as db:
        due = (
            (
                await db.execute(
                    select(ReportSchedule).where(
                        ReportSchedule.enabled.is_(True),
                        ReportSchedule.next_run_at <= now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for sched in due:
            try:
                await _dispatch_one(db, sched)
            except Exception as exc:  # noqa: BLE001 — one broken schedule shouldn't starve the rest
                sched.last_status = "failed"
                sched.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                logger.warning(
                    "report_schedule.dispatch_failed",
                    schedule_id=str(sched.id),
                    error=str(exc),
                    tb=traceback.format_exc(limit=2),
                )
            sched.last_run_at = now
            try:
                sched.next_run_at = compute_next_run(sched.cron, after=now)
            except Exception as exc:  # noqa: BLE001
                sched.last_error = f"cron unparseable: {exc}"[:1000]
                sched.enabled = False
            fired += 1
        await db.commit()
    return fired


@celery_app.task(name="reports.dispatch_schedules")
def dispatch_schedules() -> int:
    """Beat-driven entrypoint. Returns count of schedules fired (for the
    flower metric).

    Engine disposed after each tick — see poll_tunnel_health_task for
    why (asyncio.run() + module-level async engine leaks asyncpg
    connections bound to the dead loop).
    """
    async def _wrapped() -> int:
        try:
            return await _dispatch_due_async()
        finally:
            await engine.dispose()
    return asyncio.run(_wrapped())
