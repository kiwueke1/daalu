"""AI-Ops metrics + maturity level (docs/design/ai-ops-maturity.md §7, §13)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from daalu_automation.core.aiops import maturity

METRIC_KEYS = {
    "days", "alerts_total", "investigations_total", "ai_triage_rate",
    "median_time_to_diagnosis_s", "rca_agreement_rate", "verdict_coverage", "verdicts",
    "proposal_approval_rate", "actions", "ai_change_failure_rate", "escalation_rate", "auto_resolution_rate",
    "resolutions", "median_time_to_resolve_s", "runbooks_published", "runbooks_generated",
    "eval_runs", "eval_mean_score", "audit_chain_ok", "daily",
}

POLICY = """
version: 1
guardrails:
  max_auto_actions_per_hour: 5
  circuit_breaker: {cooldown_minutes: 30}
rules:
  - id: restarts
    match: {tools: [restart_deployment]}
    mode: auto_after_trust
"""


async def _seed(db, tenant_id, other_tenant_id):
    from daalu_automation.models import (
        Alert,
        AlertAction,
        EvalRun,
        Investigation,
        Runbook,
    )
    from daalu_automation.models.alert import AlertSeverity, AlertStatus
    from daalu_automation.models.alert_chat import ActionStatus, AlertChatMessage, ChatRole

    now = datetime.now(tz=timezone.utc)
    t0 = now - timedelta(hours=3)

    def alert(i, *, tid=tenant_id, created=t0, status=AlertStatus.open, resolved_at=None):
        return Alert(tenant_id=tid, module="infra", severity=AlertSeverity.warning, status=status,
                     title=f"Alert{i}", created_at=created, resolved_at=resolved_at)

    alerts = [alert(i) for i in range(6)]
    alerts[2].status = AlertStatus.resolved
    alerts[2].resolved_at = t0 + timedelta(seconds=600)
    old = alert(99, created=now - timedelta(days=40))
    other = alert(100, tid=other_tenant_id)
    db.add_all([*alerts, old, other])
    await db.flush()

    def inv(a, **kw):
        return Investigation(tenant_id=tenant_id, alert_id=a.id, started_at=t0 + timedelta(seconds=5), **kw)

    db.add_all([
        inv(alerts[0], diagnosed_at=t0 + timedelta(seconds=60), verdict="correct",
            resolution="auto", resolved_at=t0 + timedelta(seconds=120)),
        inv(alerts[1], diagnosed_at=t0 + timedelta(seconds=120), verdict="partial",
            resolution="approved", resolved_at=t0 + timedelta(seconds=300)),
        # resolved alert, investigation still open -> manual
        inv(alerts[2], diagnosed_at=t0 + timedelta(seconds=180), verdict="wrong"),
        inv(alerts[3]),
        # out of window / other tenant: ignored
        inv(old, resolution="auto", resolved_at=now - timedelta(days=39)),
    ])
    db.add(Investigation(tenant_id=other_tenant_id, alert_id=other.id, started_at=now, resolution="auto"))

    msg = AlertChatMessage(tenant_id=tenant_id, alert_id=alerts[0].id, role=ChatRole.assistant, content="x")
    db.add(msg)
    await db.flush()

    def act(n, *, status, auto=False, verified=None, write=True):
        return AlertAction(tenant_id=tenant_id, alert_id=alerts[0].id, message_id=msg.id,
                           tool_call_id=f"t{n}", tool_name="rollout_undo" if write else "list_pods",
                           requires_approval=write, status=status, auto_executed=auto,
                           verified_resolved=verified)

    db.add_all([
        act(1, status=ActionStatus.executed, auto=True, verified=True),
        act(2, status=ActionStatus.executed, verified=True),
        act(3, status=ActionStatus.rejected),
        act(4, status=ActionStatus.executed, verified=False),
        act(5, status=ActionStatus.pending),
        act(6, status=ActionStatus.executed, write=False),  # read tool: not counted
    ])
    db.add_all([
        Runbook(tenant_id=tenant_id, slug="a", title="A", status="published", source="manual"),
        Runbook(tenant_id=tenant_id, slug="b", title="B", status="draft", source="generated"),
        EvalRun(tenant_id=tenant_id, scenario_id="dummy-app-bad-image", label="m",
                started_at=now - timedelta(hours=1), score=0.8, passed=True),
        EvalRun(tenant_id=tenant_id, scenario_id="dummy-app-bad-image", label="m",
                started_at=now - timedelta(days=60), score=0.0),
    ])
    await db.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_metrics_and_levels(db, seeded):
    from daalu_automation.core.aiops import metrics
    from daalu_automation.core.aiops import policy as pol
    from daalu_automation.models import AlertAction

    tid = seeded.tenant_a_id
    await _seed(db, tid, seeded.tenant_b_id)

    m = await metrics.compute_metrics(db, tid, days=30)
    assert set(m) == METRIC_KEYS
    assert m["alerts_total"] == 6
    assert m["investigations_total"] == 4
    assert m["ai_triage_rate"] == pytest.approx(4 / 6, abs=1e-4)
    assert m["median_time_to_diagnosis_s"] == 120.0
    assert m["verdicts"] == {"correct": 1, "partial": 1, "wrong": 1}
    assert m["rca_agreement_rate"] == 0.5
    assert m["verdict_coverage"] == 0.75
    assert m["resolutions"] == {"auto": 1, "approved": 1, "manual": 1, "unresolved": 0, "open": 1}
    assert m["auto_resolution_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert m["escalation_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert m["median_time_to_resolve_s"] == 300.0  # [120, 300, 600 (manual)]
    assert m["actions"] == {"proposed": 5, "approved": 2, "rejected": 1, "auto": 1,
                            "executed": 3, "failed_verification": 1}
    assert m["proposal_approval_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert m["ai_change_failure_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert m["runbooks_published"] == 1 and m["runbooks_generated"] == 1
    assert m["eval_runs"] == 1 and m["eval_mean_score"] == 0.8
    assert m["audit_chain_ok"] is True
    assert len(m["daily"]) == 30
    today = m["daily"][-1]
    assert set(today) == {"date", "alerts", "investigations", "auto_resolved", "approved_resolved"}
    assert sum(d["alerts"] for d in m["daily"]) == 6
    assert sum(d["auto_resolved"] for d in m["daily"]) == 1
    assert sum(d["approved_resolved"] for d in m["daily"]) == 1

    # No saved policy: L3 unmet, so L4 (whose own criteria ARE met) doesn't count.
    r = await maturity.compute(db, tid)
    assert r["level"] == 2 and r["name"] == "Proposing"
    lv = {x["level"]: x for x in r["levels"]}
    assert lv[4]["met"] is True and lv[4]["achieved"] is False
    assert lv[3]["met"] is False
    crit = {c["id"]: c for c in r["criteria"]}
    assert set(crit["triage_rate"]) == {"level", "id", "label", "met", "value", "target"}
    assert not crit["auto_rule"]["met"]
    assert any("auto_after_trust" in s for s in r["next_steps"])
    assert r["assessment"] is None
    assert 12 <= len(r["questions"]) <= 18
    assert r["metrics"]["alerts_total"] == 6

    # Policy with an auto rule + guardrails, but failure rate 1/3 > 15%: still L2.
    await pol.save_policy(db, tid, POLICY, user_id=None)
    await db.commit()
    r = await maturity.compute(db, tid)
    crit = {c["id"]: c for c in r["criteria"]}
    assert crit["auto_rule"]["met"] and crit["auto_executed"]["met"]
    assert not crit["change_failure_rate"]["met"]
    assert r["level"] == 2

    # Fix the failure → L3 met → L4 (already met) counts. L5 not (auto-resolution 33%).
    await db.execute(update(AlertAction).where(AlertAction.tool_call_id == "t4")
                     .values(verified_resolved=True))
    await db.commit()
    r = await maturity.compute(db, tid)
    assert r["level"] == 4 and r["name"] == "Learning"
    crit = {c["id"]: c for c in r["criteria"]}
    assert crit["guardrails"]["met"] and crit["audit_chain_ok"]["met"]
    assert not crit["escalation_rate"]["met"] and not crit["rca_agreement_rate"]["met"]
    assert r["next_steps"] and all(isinstance(s, str) for s in r["next_steps"])


@pytest.mark.asyncio(loop_scope="session")
async def test_empty_tenant_is_level_0(db, seeded):
    from daalu_automation.core.aiops import metrics

    m = await metrics.compute_metrics(db, seeded.tenant_a_id, days=7)
    assert m["alerts_total"] == 0 and m["ai_triage_rate"] is None
    assert m["rca_agreement_rate"] is None and m["ai_change_failure_rate"] is None
    assert len(m["daily"]) == 7
    r = await maturity.compute(db, seeded.tenant_a_id, days=7)
    assert r["level"] == 0 and r["name"] == "Ad hoc"
    assert r["next_steps"]


def test_level_rules_are_cumulative():
    def crit(met_by_level):
        return [{"level": lv, "met": met} for lv, mets in met_by_level.items() for met in mets]

    assert maturity.level_from_criteria(crit({1: [True], 2: [True], 3: [False], 4: [True], 5: [True]})) == 2
    assert maturity.level_from_criteria(crit({1: [True, False], 2: [True]})) == 0
    assert maturity.level_from_criteria(crit({1: [True], 2: [True], 3: [True], 4: [True], 5: [True]})) == 5

    l1_l2 = {q["id"]: True for q in maturity.QUESTIONS if q["level"] <= 2}
    assert maturity.self_assessed_level(l1_l2) == 2
    all_but_l3 = {q["id"]: q["level"] != 3 for q in maturity.QUESTIONS}
    assert maturity.self_assessed_level(all_but_l3) == 2
    assert maturity.self_assessed_level({}) == 0
    assert {q["level"] for q in maturity.QUESTIONS} == {1, 2, 3, 4, 5}


@pytest.mark.asyncio(loop_scope="session")
async def test_metrics_maturity_api(client, db, seeded):
    h = {"Authorization": f"Bearer {seeded.token_a}"}
    r = await client.get("/api/v1/aiops/metrics?days=14", headers=h)
    assert r.status_code == 200 and set(r.json()) == METRIC_KEYS and r.json()["days"] == 14

    answers = {q["id"]: q["level"] <= 2 for q in maturity.QUESTIONS}
    r = await client.post("/api/v1/aiops/maturity/assessment", headers=h, json={"answers": answers})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["level"] == 2 and a["created_by"] == "alice@tenant-a.example"
    assert {"level", "answers", "created_at", "created_by"} <= set(a)

    r = await client.post("/api/v1/aiops/maturity/assessment", headers=h,
                          json={"answers": {"not_a_question": True}})
    assert r.status_code == 422

    r = await client.get("/api/v1/aiops/maturity", headers=h)
    body = r.json()
    assert {"level", "name", "levels", "criteria", "next_steps", "metrics", "assessment",
            "questions"} <= set(body)
    assert body["assessment"]["level"] == 2
    assert len(body["levels"]) == 6

    # Tenant B has no assessment.
    r = await client.get("/api/v1/aiops/maturity",
                         headers={"Authorization": f"Bearer {seeded.token_b}"})
    assert r.json()["assessment"] is None
