"""AI-Ops metrics (docs/design/ai-ops-maturity.md §7, shape in §13).

The DORA-style numbers behind the maturity level: how much of the alert load
the AI triages, how fast it triages, how often humans agree with it, how often its changes
are approved and how often they fail, how much still needs a human.

Window: alerts created in the last ``days`` days; investigations of those
alerts; write actions proposed in the window; eval runs started in the window.
Runbook counts are current totals. Rates are 0..1 floats, or ``None`` when the
denominator is 0.

An investigation whose alert has been resolved while the investigation is
still ``open`` (nobody ran a fix through Daalu) counts as ``manual``, resolved
at the alert's ``resolved_at``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.aiops import audit

RESOLUTIONS = ("auto", "approved", "manual", "unresolved", "open")
RESOLVED = ("auto", "approved", "manual")


def _rate(num: float, den: float) -> float | None:
    return round(num / den, 4) if den else None


def _secs(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return max(0.0, (a - b).total_seconds())


def _median(xs: list[float]) -> float | None:
    return round(float(median(xs)), 1) if xs else None


def effective_resolution(inv: Any, alert: Any) -> tuple[str, datetime | None]:
    """``(resolution, resolved_at)`` with the ``open``-but-alert-resolved →
    ``manual`` rule applied."""
    res = inv.resolution or "open"
    at = inv.resolved_at
    status = getattr(alert.status, "value", alert.status)
    if res == "open" and status == "resolved":
        return "manual", alert.resolved_at or alert.updated_at
    if res == "manual" and at is None:
        at = alert.resolved_at
    return res, at


def _day(dt: datetime | None) -> date | None:
    return dt.astimezone(timezone.utc).date() if dt else None


async def collect(db: AsyncSession, tenant_id: uuid.UUID, days: int = 30) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(metrics, extras)``. ``extras`` carries counts the maturity criteria
    need that aren't part of the public metrics shape."""
    from daalu_automation.models import (
        Alert,
        AlertAction,
        EvalRun,
        Investigation,
        Runbook,
    )
    from daalu_automation.models.alert_chat import ActionStatus

    days = max(1, min(int(days), 365))
    now = datetime.now(tz=timezone.utc)
    since = now - timedelta(days=days)

    alerts = (
        await db.execute(
            select(Alert).where(Alert.tenant_id == tenant_id, Alert.created_at >= since)
        )
    ).scalars().all()
    alerts_by_id = {a.id: a for a in alerts}

    invs = (
        await db.execute(
            select(Investigation, Alert)
            .join(Alert, Alert.id == Investigation.alert_id)
            .where(Investigation.tenant_id == tenant_id, Alert.created_at >= since)
        )
    ).all()

    # Triage, verdicts, resolutions.
    ttt: list[float] = []
    ttr: list[float] = []
    verdicts = {"correct": 0, "partial": 0, "wrong": 0}
    resolutions = dict.fromkeys(RESOLUTIONS, 0)
    triaged_alerts: set[uuid.UUID] = set()
    daily_inv: dict[date, int] = {}
    daily_auto: dict[date, int] = {}
    daily_appr: dict[date, int] = {}
    for inv, alert in invs:
        triaged_alerts.add(alert.id)
        d = _day(inv.started_at)
        if d:
            daily_inv[d] = daily_inv.get(d, 0) + 1
        if inv.triaged_at is not None:
            s = _secs(inv.triaged_at, alert.created_at)
            if s is not None:
                ttt.append(s)
        if inv.verdict in verdicts:
            verdicts[inv.verdict] += 1
        res, at = effective_resolution(inv, alert)
        resolutions[res if res in resolutions else "open"] += 1
        if res in RESOLVED:
            s = _secs(at, alert.created_at)
            if s is not None:
                ttr.append(s)
            rd = _day(at)
            if rd and res == "auto":
                daily_auto[rd] = daily_auto.get(rd, 0) + 1
            elif rd and res == "approved":
                daily_appr[rd] = daily_appr.get(rd, 0) + 1

    investigations_total = len(invs)
    n_verdicts = sum(verdicts.values())
    n_resolved = sum(resolutions[r] for r in RESOLVED)

    # Write actions proposed in the window.
    acts = (
        await db.execute(
            select(AlertAction).where(
                AlertAction.tenant_id == tenant_id,
                AlertAction.requires_approval.is_(True),
                AlertAction.created_at >= since,
            )
        )
    ).scalars().all()
    ran = (ActionStatus.executed, ActionStatus.failed)
    human = [a for a in acts if not a.auto_executed]
    approved = sum(1 for a in human if a.status in (ActionStatus.approved, *ran))
    rejected = sum(1 for a in human if a.status == ActionStatus.rejected)
    executed = [a for a in acts if a.status in ran]
    failed_verification = sum(1 for a in executed if a.verified_resolved is False)
    auto_n = sum(1 for a in acts if a.auto_executed)
    extras = {
        "human_executed": sum(1 for a in executed if not a.auto_executed),
        "auto_executed": sum(1 for a in executed if a.auto_executed),
    }

    # Runbooks (current totals) and evals (window).
    rb_rows = (
        await db.execute(
            select(Runbook.status, Runbook.source, func.count())
            .where(Runbook.tenant_id == tenant_id)
            .group_by(Runbook.status, Runbook.source)
        )
    ).all()
    runbooks_published = sum(n for st, _src, n in rb_rows if st == "published")
    runbooks_generated = sum(n for _st, src, n in rb_rows if src == "generated")

    # Runs marked invalid (e.g. answered by the wrong model) don't count,
    # matching the leaderboard.
    ev_rows = (
        await db.execute(
            select(EvalRun.score, EvalRun.details).where(
                EvalRun.tenant_id == tenant_id, EvalRun.started_at >= since
            )
        )
    ).all()
    valid_scores = [
        float(score) for score, details in ev_rows
        if not (isinstance(details, dict) and details.get("invalid"))
    ]
    eval_runs = len(valid_scores)
    eval_mean = round(sum(valid_scores) / eval_runs, 4) if eval_runs else None

    chain = await audit.verify_chain(db, tenant_id)

    # Daily series, oldest first, one entry per UTC day ending today.
    daily_alerts: dict[date, int] = {}
    for a in alerts:
        d = _day(a.created_at)
        if d:
            daily_alerts[d] = daily_alerts.get(d, 0) + 1
    today = now.date()
    daily = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        daily.append({
            "date": d.isoformat(),
            "alerts": daily_alerts.get(d, 0),
            "investigations": daily_inv.get(d, 0),
            "auto_resolved": daily_auto.get(d, 0),
            "approved_resolved": daily_appr.get(d, 0),
        })

    metrics = {
        "days": days,
        "alerts_total": len(alerts),
        "investigations_total": investigations_total,
        "ai_triage_rate": _rate(len(triaged_alerts & alerts_by_id.keys()), len(alerts)),
        "median_time_to_triage_s": _median(ttt),
        "agreement_rate": _rate(verdicts["correct"] + 0.5 * verdicts["partial"], n_verdicts),
        "verdict_coverage": _rate(n_verdicts, investigations_total),
        "verdicts": verdicts,
        "proposal_approval_rate": _rate(approved, approved + rejected),
        "actions": {
            "proposed": len(acts),
            "approved": approved,
            "rejected": rejected,
            "auto": auto_n,
            "executed": len(executed),
            "failed_verification": failed_verification,
        },
        "change_failure_rate": _rate(failed_verification, len(executed)),
        # TRACE "E": resolved incidents that still needed a human (approved or manual).
        "escalation_rate": _rate(n_resolved - resolutions["auto"], n_resolved),
        "resolutions": resolutions,
        "median_time_to_resolve_s": _median(ttr),
        "runbooks_published": runbooks_published,
        "runbooks_generated": runbooks_generated,
        "eval_runs": eval_runs,
        "eval_mean_score": eval_mean,
        "audit_chain_ok": bool(chain.get("ok")),
        "daily": daily,
    }
    return metrics, extras


async def compute_metrics(db: AsyncSession, tenant_id: uuid.UUID, days: int = 30) -> dict[str, Any]:
    metrics, _ = await collect(db, tenant_id, days)
    return metrics
