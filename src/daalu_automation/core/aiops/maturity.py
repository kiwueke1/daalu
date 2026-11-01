"""AI-Ops maturity level (docs/design/ai-ops-maturity.md §1, §7, shape §13).

Two views, reported side by side:

* the **automatic level**, computed from data: the highest level whose
  criteria are met *and* every lower level's criteria are met (levels are
  cumulative, so meeting L4's criteria without L3's leaves a team at L2);
* the **self-assessed level**, from yes/no answers about the organisational
  practices the data can't see. Same rule: the highest level whose questions,
  and every lower level's, are all answered yes (unanswered = no).

The headline ``level`` is the automatic one.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.aiops import metrics as metrics_mod
from daalu_automation.core.aiops import policy as policy_mod

LEVELS: list[dict[str, Any]] = [
    {"level": 0, "name": "Ad hoc",
     "description": "Individuals paste logs into a chatbot. No shared context, no record."},
    {"level": 1, "name": "Assisted",
     "description": "AI reads alerts, logs, metrics and explains them. Read-only."},
    {"level": 2, "name": "Proposing",
     "description": "AI names the root cause and proposes a fix; every change is human-approved."},
    {"level": 3, "name": "Trusted",
     "description": "Policy lets low-risk, proven actions run automatically."},
    {"level": 4, "name": "Learning",
     "description": "Every incident improves the next: verdicts, runbooks, evals, metrics."},
    {"level": 5, "name": "Autonomous",
     "description": "AI resolves most routine incidents; humans own policy and exceptions."},
]
LEVEL_NAMES = {lv["level"]: lv["name"] for lv in LEVELS}

QUESTIONS: list[dict[str, Any]] = [
    # L1 — Assisted
    {"id": "alerts_routed", "level": 1,
     "text": "Production alerts reach Daalu (or your AI tool) automatically, not by copy-paste."},
    {"id": "read_access_shared", "level": 1,
     "text": "The AI has read access to the same logs, metrics and cluster state on-call uses."},
    {"id": "no_private_chatbots", "level": 1,
     "text": "Investigations happen in a shared, recorded place instead of personal chatbot sessions."},
    # L2 — Proposing
    {"id": "oncall_reviews_rca", "level": 2,
     "text": "On-call reviews the AI's root cause before acting on it."},
    {"id": "changes_via_approval", "level": 2,
     "text": "Fixes the AI proposes are applied through the approval flow, not by hand on the side."},
    {"id": "rejections_explained", "level": 2,
     "text": "When a proposal is rejected, the reason is written down."},
    # L3 — Trusted
    {"id": "policy_in_review", "level": 3,
     "text": "The autonomy policy is changed through code review and reviewed at least quarterly."},
    {"id": "risk_tiers_agreed", "level": 3,
     "text": "The team has agreed which actions are low-risk enough to run automatically, per environment."},
    {"id": "kill_switch_known", "level": 3,
     "text": "Everyone on call knows how to use the kill switch, and it has been tested."},
    # L4 — Learning
    {"id": "pir_includes_ai", "level": 4,
     "text": "Post-incident reviews include the AI's investigation and a verdict on it."},
    {"id": "runbooks_maintained", "level": 4,
     "text": "Runbooks are kept as code and updated after incidents (including AI-drafted ones)."},
    {"id": "evals_before_change", "level": 4,
     "text": "Model or prompt changes are evaluated against the scenario suite before rollout."},
    {"id": "metrics_reviewed", "level": 4,
     "text": "AI-Ops metrics (agreement, change failure, auto-resolution) are reviewed on a regular cadence."},
    # L5 — Autonomous
    {"id": "page_on_exceptions", "level": 5,
     "text": "Humans are paged only for exceptions the policy can't handle."},
    {"id": "audit_reviewed", "level": 5,
     "text": "The audit log is verified and sampled regularly (security/compliance owns it)."},
    {"id": "autonomy_budget", "level": 5,
     "text": "There's an explicit error budget for automatic changes; breaching it demotes autonomy."},
]
QUESTION_IDS = {q["id"] for q in QUESTIONS}


def self_assessed_level(answers: dict[str, bool]) -> int:
    level = 0
    for lv in range(1, 6):
        qs = [q for q in QUESTIONS if q["level"] == lv]
        if qs and all(bool(answers.get(q["id"])) for q in qs):
            level = lv
        else:
            break
    return level


def _fmt(v: Any) -> str:
    if v is None:
        return "unknown"
    if isinstance(v, float):
        return f"{v:.0%}" if 0 <= v <= 1 else f"{v:g}"
    return str(v)


def build_criteria(m: dict[str, Any], ex: dict[str, Any], pol: dict[str, Any]) -> list[dict[str, Any]]:
    """Every criterion: ``{level, id, label, met, value, target}`` plus a
    ``step`` (the next-step text used when unmet). Pure."""

    def c(level: int, cid: str, label: str, met: bool, value: Any, target: str, step: str) -> dict[str, Any]:
        return {"level": level, "id": cid, "label": label, "met": bool(met),
                "value": value, "target": target, "step": step}

    tr, n = m["ai_triage_rate"], m["alerts_total"]
    par = m["proposal_approval_rate"]
    cfr = m["ai_change_failure_rate"]
    vc = m["verdict_coverage"]
    arr = m["auto_resolution_rate"]
    rca = m["rca_agreement_rate"]
    return [
        c(1, "triage_rate", "Alerts investigated by the AI", tr is not None and tr >= 0.5, tr, "≥ 50%",
          f"AI triage covers {_fmt(tr)} of alerts. Autonomy policy → set "
          "`investigate_automatically: true` so every new alert is investigated."),
        c(1, "alert_volume", "Alerts in the window", n >= 5, n, "≥ 5",
          f"Only {n} alert(s) reached Daalu in the window. Integrations → connect "
          "Prometheus/Alertmanager (or another alert source) so real alerts flow in."),
        c(2, "human_approved_executed", "Human-approved fixes executed",
          ex["human_executed"] >= 1, ex["human_executed"], "≥ 1",
          "Open an alert → let the AI propose a fix → Approve it in the chat, so a "
          "human-approved change runs and is verified."),
        c(2, "approval_rate_known", "Proposal approval rate measured", par is not None, par, "known",
          "Approve or reject the AI's pending proposals in alert chats; the approval "
          "rate needs at least one decided proposal."),
        c(3, "auto_rule", "Policy rules allowing automatic actions",
          pol["auto_rules"] >= 1, pol["auto_rules"], "≥ 1 auto / auto_after_trust rule",
          "Autonomy policy → add an `auto_after_trust` rule for a low-risk action you "
          "approve often (e.g. `restart_deployment` or `scale_deployment`)."),
        c(3, "auto_executed", "Automatic actions executed",
          ex["auto_executed"] >= 1, ex["auto_executed"], "≥ 1",
          "No automatic action has run yet. Check Autonomy policy → Trust: an "
          "`auto_after_trust` rule turns automatic once it has enough verified successes."),
        c(3, "change_failure_rate", "AI change failure rate",
          cfr is not None and cfr <= 0.15, cfr, "≤ 15%",
          f"AI change failure rate is {_fmt(cfr)}. Investigations → review the fixes that "
          "failed verification; narrow or demote the rules that produced them."),
        c(4, "verdict_coverage", "Investigations with a human verdict",
          vc is not None and vc >= 0.3, vc, "≥ 30%",
          f"Verdict coverage is {_fmt(vc)}. Investigations → filter 'needs verdict' and "
          "mark root causes correct / partial / wrong."),
        c(4, "runbooks_published", "Published runbooks",
          m["runbooks_published"] >= 1, m["runbooks_published"], "≥ 1",
          "Runbooks → publish a runbook (review an AI-generated draft or import your "
          "existing markdown runbooks)."),
        c(4, "eval_runs", "Eval runs in the window", m["eval_runs"] >= 1, m["eval_runs"], "≥ 1",
          "Evals → run a scenario: `daalu eval run dummy-app-bad-image --label <model>` "
          "against a test cluster."),
        c(5, "auto_resolution_rate", "Resolved automatically",
          arr is not None and arr >= 0.5, arr, "≥ 50%",
          f"{_fmt(arr)} of resolved incidents were fixed automatically. Autonomy policy → "
          "extend auto / auto_after_trust rules to your most frequent proven fixes."),
        c(5, "rca_agreement_rate", "Root-cause agreement",
          rca is not None and rca >= 0.8, rca, "≥ 80%",
          f"Root-cause agreement is {_fmt(rca)}. Runbooks → add runbooks for the alerts the "
          "AI gets wrong, and use Evals to compare models before switching."),
        c(5, "audit_chain_ok", "Audit chain verifies", m["audit_chain_ok"], m["audit_chain_ok"], "verifies",
          "Audit log → Verify: the hash chain is broken; investigate who edited audit rows."),
        c(5, "guardrails", "Guardrails configured (circuit breaker + rate limit)",
          pol["guardrails"], pol["guardrails"], "circuit breaker + hourly rate limit",
          "Autonomy policy → set `guardrails.circuit_breaker` and "
          "`guardrails.max_auto_actions_per_hour` in your saved policy."),
    ]


def level_from_criteria(criteria: list[dict[str, Any]]) -> int:
    level = 0
    for lv in range(1, 6):
        cs = [c for c in criteria if c["level"] == lv]
        if cs and all(c["met"] for c in cs):
            level = lv
        else:
            break
    return level


async def _policy_facts(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    """Facts from the *saved* policy. No saved policy means the built-in
    approve-everything default: no auto rules and no guardrails chosen."""
    row = await policy_mod.load_policy_row(db, tenant_id)
    if row is None:
        return {"saved": False, "auto_rules": 0, "guardrails": False}
    try:
        doc = policy_mod.parse_policy(row.yaml)
    except policy_mod.PolicyError:
        return {"saved": True, "auto_rules": 0, "guardrails": False}
    auto_rules = sum(1 for r in doc.rules if r.mode in ("auto", "auto_after_trust"))
    if doc.defaults.mode in ("auto", "auto_after_trust"):
        auto_rules += 1
    g = doc.guardrails
    guard = g.circuit_breaker is not None and bool(g.max_auto_actions_per_hour)
    return {"saved": True, "auto_rules": auto_rules, "guardrails": guard}


async def latest_assessment(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any] | None:
    from daalu_automation.models import MaturityAssessment

    row = (
        await db.execute(
            select(MaturityAssessment)
            .where(MaturityAssessment.tenant_id == tenant_id)
            .order_by(MaturityAssessment.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return assessment_out(row) if row else None


def assessment_out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "level": row.level,
        "name": LEVEL_NAMES.get(row.level, ""),
        "answers": row.answers or {},
        "created_at": row.created_at,
        "created_by": row.created_by,
    }


async def save_assessment(db: AsyncSession, tenant_id: uuid.UUID, answers: dict[str, bool],
                          *, created_by: str) -> dict[str, Any]:
    from daalu_automation.models import MaturityAssessment

    unknown = sorted(set(answers) - QUESTION_IDS)
    if unknown:
        raise ValueError(f"unknown question id(s): {unknown}")
    clean = {q["id"]: bool(answers.get(q["id"], False)) for q in QUESTIONS}
    row = MaturityAssessment(
        tenant_id=tenant_id, answers=clean, level=self_assessed_level(clean),
        created_by=created_by[:255],
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return assessment_out(row)


def next_steps(criteria: list[dict[str, Any]], level: int, *, limit: int = 6) -> list[str]:
    """Unmet criteria of the next level first, then of the level after."""
    steps: list[str] = []
    for lv in (level + 1, level + 2):
        steps += [c["step"] for c in criteria if c["level"] == lv and not c["met"]]
    if not steps and level >= 5:
        steps.append("You're at Level 5. Keep verdict coverage and evals running so the "
                     "level holds; review the autonomy policy quarterly.")
    return steps[:limit]


async def compute(db: AsyncSession, tenant_id: uuid.UUID, days: int = 30) -> dict[str, Any]:
    m, ex = await metrics_mod.collect(db, tenant_id, days)
    pol = await _policy_facts(db, tenant_id)
    crit = build_criteria(m, ex, pol)
    level = level_from_criteria(crit)
    levels = []
    for lv in LEVELS:
        own = [c for c in crit if c["level"] == lv["level"]]
        levels.append({
            **lv,
            "met": all(c["met"] for c in own),  # this level's own criteria
            "achieved": lv["level"] <= level,   # cumulative
        })
    return {
        "level": level,
        "name": LEVEL_NAMES[level],
        "levels": levels,
        "criteria": [{k: v for k, v in c.items() if k != "step"} for c in crit],
        "next_steps": next_steps(crit, level),
        "metrics": m,
        "assessment": await latest_assessment(db, tenant_id),
        "questions": [dict(q) for q in QUESTIONS],
    }
