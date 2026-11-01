"""End-to-end AOMM flow through the alert chat loop (DB-backed).

Covers: policy ``auto`` runs a write without a human and verifies it; a
failed automatic fix trips the circuit breaker; multi-approver approvals;
``deny``; the investigation record; verdicts; and the audit hash chain.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import select, update

REASON = "Roll back to the last healthy revision."


pytestmark = pytest.mark.usefixtures("no_aiops_side_calls")


def _tool_use(i: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": i, "name": name, "input": args}


SUMMARY = {
    "content": [{
        "type": "text",
        "text": "## Root cause\nBad image tag in revision 4.\n\n## Background\n—\n\n"
                "## Remediation plan\n1. **[system]** Roll back.",
    }],
    "stop_reason": "end_turn",
}


async def _setup(db, seeded, policy_yaml: str | None):
    from daalu_automation.core.aiops import policy as pol
    from daalu_automation.models import Alert
    from daalu_automation.models.alert import AlertSeverity, AlertStatus
    from daalu_automation.models.alert_chat import AlertChatMessage, ChatRole

    if policy_yaml is not None:
        await pol.save_policy(db, seeded.tenant_a_id, policy_yaml, user_id=None)
    alert = Alert(
        tenant_id=seeded.tenant_a_id, module="infra", severity=AlertSeverity.warning,
        status=AlertStatus.open, title="DummyAppDown", body="dummy-app down",
        metadata_json={"cluster": "dev-1", "namespace": "demo"},
    )
    db.add(alert)
    await db.flush()
    db.add(AlertChatMessage(
        tenant_id=seeded.tenant_a_id, alert_id=alert.id, role=ChatRole.tool,
        content="pods CrashLoopBackOff", tool_call_id="prior",
    ))
    await db.commit()
    return alert


def _replies(monkeypatch, replies):
    from daalu_automation.api.routers import alert_chat

    async def fake_ask_model(**_: Any) -> dict[str, Any]:
        return replies.pop(0) if replies else SUMMARY

    monkeypatch.setattr(alert_chat, "_ask_model", fake_ask_model)


def _kube(monkeypatch, *, recovered: bool):
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.core import kube_tools

    state = "desired=1 ready=1 available=1" if recovered else "desired=1 ready=0 available=0"

    async def fake_execute(name: str, tool_input: dict[str, Any], **_: Any) -> str:
        if name == "get_deployment":
            return f"deployment demo/dummy-app {state}"
        return f"{name} ok"

    async def fake_wait(**_: Any) -> str:
        return f"deployment demo/dummy-app {state}"

    monkeypatch.setattr(kube_tools, "execute_tool", fake_execute)
    monkeypatch.setattr(alert_chat, "_wait_for_deployment", fake_wait)


AUTO_POLICY = """
version: 1
guardrails:
  circuit_breaker: {cooldown_minutes: 30}
rules:
  - id: dev-rollback
    match: {tools: [rollout_undo], clusters: ["dev-*"]}
    mode: auto
"""


@pytest.mark.asyncio(loop_scope="session")
async def test_auto_policy_runs_and_verifies_without_a_human(db, seeded, monkeypatch):
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.core.aiops import audit
    from daalu_automation.models import AlertAction, Investigation
    from daalu_automation.models.alert_chat import ActionStatus

    alert = await _setup(db, seeded, AUTO_POLICY)
    _kube(monkeypatch, recovered=True)
    _replies(monkeypatch, [{
        "content": [_tool_use("w1", "rollout_undo",
                              {"namespace": "demo", "name": "dummy-app", "reason": REASON})],
        "stop_reason": "tool_use",
    }])
    await alert_chat._record_assistant_turn(db=db, tenant_id=seeded.tenant_a_id, alert=alert)
    await alert_chat.drain_auto_tasks()

    alert_id = alert.id
    db.expire_all()
    action = (await db.execute(select(AlertAction).where(
        AlertAction.alert_id == alert_id, AlertAction.requires_approval.is_(True)))).scalar_one()
    assert action.auto_executed and action.policy_rule == "dev-rollback"
    assert action.status is ActionStatus.executed
    assert action.verified_resolved is True
    inv = (await db.execute(select(Investigation).where(
        Investigation.alert_id == alert_id))).scalar_one()
    assert inv.resolution == "auto" and inv.actions_auto == 1 and inv.verified is True
    assert inv.root_cause.startswith("Bad image tag")
    assert inv.diagnosed_at is not None

    events = (await audit.verify_chain(db, seeded.tenant_a_id))
    assert events["ok"] and events["checked"] == 3  # proposed, auto_executed, verified


@pytest.mark.asyncio(loop_scope="session")
async def test_failed_auto_fix_trips_breaker(db, seeded, monkeypatch):
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.core.aiops import policy as pol

    alert = await _setup(db, seeded, AUTO_POLICY)
    _kube(monkeypatch, recovered=False)
    _replies(monkeypatch, [{
        "content": [_tool_use("w1", "rollout_undo",
                              {"namespace": "demo", "name": "dummy-app", "reason": REASON})],
        "stop_reason": "tool_use",
    }])
    await alert_chat._record_assistant_turn(db=db, tenant_id=seeded.tenant_a_id, alert=alert)
    await alert_chat.drain_auto_tasks()

    db.expire_all()
    d = await pol.decide_for_tenant(db, seeded.tenant_a_id, pol.WriteRequest(
        tool="rollout_undo", cluster="dev-1", namespace="demo"))
    assert d.mode == "approve" and "circuit breaker" in d.reason


@pytest.mark.asyncio(loop_scope="session")
async def test_deny_never_creates_an_action(db, seeded, monkeypatch):
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.models import AlertAction, AlertChatMessage
    from daalu_automation.models.alert_chat import ChatRole

    alert = await _setup(db, seeded, "rules: [{id: nope, match: {tools: [rollout_undo]}, mode: deny}]")
    _kube(monkeypatch, recovered=True)
    _replies(monkeypatch, [{
        "content": [_tool_use("w1", "rollout_undo",
                              {"namespace": "demo", "name": "dummy-app", "reason": REASON})],
        "stop_reason": "tool_use",
    }] * 3)
    await alert_chat._record_assistant_turn(db=db, tenant_id=seeded.tenant_a_id, alert=alert)
    writes = (await db.execute(select(AlertAction).where(
        AlertAction.alert_id == alert.id, AlertAction.requires_approval.is_(True)))).scalars().all()
    assert writes == []
    msgs = (await db.execute(select(AlertChatMessage).where(
        AlertChatMessage.alert_id == alert.id, AlertChatMessage.role == ChatRole.tool,
        AlertChatMessage.tool_call_id == "w1"))).scalars().all()
    assert msgs and "denied by the autonomy policy" in msgs[0].content


@pytest.mark.asyncio(loop_scope="session")
async def test_two_approvers_required(db, seeded, monkeypatch):
    from fastapi import HTTPException

    from daalu_automation.api.routers import alert_chat
    from daalu_automation.models import AlertAction, User

    alert = await _setup(
        db, seeded, "rules: [{id: two, match: {tools: [rollout_undo]}, mode: approve, approvers: 2}]"
    )
    _kube(monkeypatch, recovered=True)
    _replies(monkeypatch, [{
        "content": [_tool_use("w1", "rollout_undo",
                              {"namespace": "demo", "name": "dummy-app", "reason": REASON})],
        "stop_reason": "tool_use",
    }])
    await alert_chat._record_assistant_turn(db=db, tenant_id=seeded.tenant_a_id, alert=alert)
    action = (await db.execute(select(AlertAction).where(
        AlertAction.alert_id == alert.id, AlertAction.requires_approval.is_(True)))).scalar_one()
    assert action.approvals_required == 2 and action.policy_mode == "approve"

    user_a = await db.get(User, seeded.user_a_id)
    second = User(tenant_id=seeded.tenant_a_id, email="second@a.test", is_active=True)
    db.add(second)
    await db.commit()

    tasks = BackgroundTasks()
    out = await alert_chat.approve_action(
        alert_id=str(alert.id), action_id=str(action.id), background_tasks=tasks,
        db=db, tenant_id=seeded.tenant_a_id, user=user_a,
    )
    assert out.workflow_run_id == "" and out.approvals_remaining == 1 and not tasks.tasks
    with pytest.raises(HTTPException) as e:
        await alert_chat.approve_action(
            alert_id=str(alert.id), action_id=str(action.id), background_tasks=tasks,
            db=db, tenant_id=seeded.tenant_a_id, user=user_a,
        )
    assert e.value.status_code == 409
    out = await alert_chat.approve_action(
        alert_id=str(alert.id), action_id=str(action.id), background_tasks=tasks,
        db=db, tenant_id=seeded.tenant_a_id, user=second,
    )
    assert out.workflow_run_id and len(tasks.tasks) == 1
    action_id = action.id
    await tasks()
    db.expire_all()
    action = await db.get(AlertAction, action_id)
    assert action.verified_resolved is True and len(action.approvals) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_verdict_and_tamper_detection(db, seeded, monkeypatch):
    from daalu_automation.api.routers import aiops_investigations
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.core.aiops import audit
    from daalu_automation.models import AuditEvent, Investigation, User

    alert = await _setup(db, seeded, None)
    _kube(monkeypatch, recovered=True)
    _replies(monkeypatch, [SUMMARY])
    await alert_chat._record_assistant_turn(db=db, tenant_id=seeded.tenant_a_id, alert=alert)
    inv = (await db.execute(select(Investigation).where(
        Investigation.alert_id == alert.id))).scalar_one()

    user_a = await db.get(User, seeded.user_a_id)
    out = await aiops_investigations.submit_verdict(
        str(inv.id), aiops_investigations.VerdictIn(verdict="correct", note="spot on"),
        db=db, tenant_id=seeded.tenant_a_id, user=user_a,
    )
    assert out["verdict"] == "correct"
    assert (await audit.verify_chain(db, seeded.tenant_a_id))["ok"]

    await db.execute(update(AuditEvent).where(
        AuditEvent.tenant_id == seeded.tenant_a_id, AuditEvent.seq == 1,
    ).values(actor="someone-else"))
    await db.commit()
    res = await audit.verify_chain(db, seeded.tenant_a_id)
    assert not res["ok"] and res["broken_at"] == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_hourly_cap_counts_writes_in_the_same_turn(db, seeded, monkeypatch):
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.models import AlertAction

    policy = (
        "guardrails: {max_auto_actions_per_hour: 1}\n"
        "rules: [{id: dev, match: {clusters: ['dev-*']}, mode: auto}]\n"
    )
    alert = await _setup(db, seeded, policy)
    _kube(monkeypatch, recovered=True)
    _replies(monkeypatch, [{
        "content": [
            _tool_use("w1", "restart_deployment",
                      {"namespace": "demo", "name": "a", "reason": REASON}),
            _tool_use("w2", "restart_deployment",
                      {"namespace": "demo", "name": "b", "reason": REASON}),
        ],
        "stop_reason": "tool_use",
    }])
    alert_id = alert.id
    await alert_chat._record_assistant_turn(db=db, tenant_id=seeded.tenant_a_id, alert=alert)
    await alert_chat.drain_auto_tasks()
    db.expire_all()
    modes = sorted(
        a.policy_mode for a in (await db.execute(select(AlertAction).where(
            AlertAction.alert_id == alert_id, AlertAction.requires_approval.is_(True)
        ))).scalars()
    )
    assert modes == ["approve", "auto"]


@pytest.mark.asyncio(loop_scope="session")
async def test_mcp_proposer_cannot_approve_own_action(db, seeded, monkeypatch):
    from fastapi import HTTPException

    from daalu_automation.api.routers import alert_chat
    from daalu_automation.models import AlertAction, User

    alert = await _setup(db, seeded, None)
    _kube(monkeypatch, recovered=True)
    _replies(monkeypatch, [{
        "content": [_tool_use("w1", "rollout_undo",
                              {"namespace": "demo", "name": "dummy-app", "reason": REASON})],
        "stop_reason": "tool_use",
    }])
    await alert_chat._record_assistant_turn(db=db, tenant_id=seeded.tenant_a_id, alert=alert)
    user_a = await db.get(User, seeded.user_a_id)
    action = (await db.execute(select(AlertAction).where(
        AlertAction.alert_id == alert.id, AlertAction.requires_approval.is_(True)))).scalar_one()
    action.actor = f"mcp:{user_a.email}"
    await db.commit()
    with pytest.raises(HTTPException) as e:
        await alert_chat.approve_action(
            alert_id=str(alert.id), action_id=str(action.id),
            background_tasks=BackgroundTasks(), db=db,
            tenant_id=seeded.tenant_a_id, user=user_a,
        )
    assert e.value.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_crash_during_auto_fix_counts_as_failure(db, seeded, monkeypatch):
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.core import kube_tools
    from daalu_automation.core.aiops import policy as pol
    from daalu_automation.models import AlertAction

    alert = await _setup(db, seeded, AUTO_POLICY)

    async def boom(name: str, tool_input: dict[str, Any], **_: Any) -> str:
        raise RuntimeError("apiserver went away")

    monkeypatch.setattr(kube_tools, "execute_tool", boom)
    _replies(monkeypatch, [{
        "content": [_tool_use("w1", "rollout_undo",
                              {"namespace": "demo", "name": "dummy-app", "reason": REASON})],
        "stop_reason": "tool_use",
    }])
    alert_id = alert.id
    await alert_chat._record_assistant_turn(db=db, tenant_id=seeded.tenant_a_id, alert=alert)
    await alert_chat.drain_auto_tasks()
    db.expire_all()
    action = (await db.execute(select(AlertAction).where(
        AlertAction.alert_id == alert_id, AlertAction.requires_approval.is_(True)))).scalar_one()
    assert action.verified_resolved is False
    d = await pol.decide_for_tenant(db, seeded.tenant_a_id, pol.WriteRequest(
        tool="rollout_undo", cluster="dev-1", namespace="demo"))
    assert "circuit breaker" in d.reason


@pytest.mark.asyncio(loop_scope="session")
async def test_invalid_eval_runs_do_not_count(db, seeded):
    from datetime import datetime, timezone

    from daalu_automation.core.aiops import metrics
    from daalu_automation.models import EvalRun

    now = datetime.now(tz=timezone.utc)
    db.add_all([
        EvalRun(tenant_id=seeded.tenant_a_id, scenario_id="s", label="m", started_at=now,
                score=1.0, passed=True, details={}),
        EvalRun(tenant_id=seeded.tenant_a_id, scenario_id="s", label="m", started_at=now,
                score=0.0, passed=False, details={"invalid": "wrong model"}),
    ])
    await db.commit()
    out = await metrics.compute_metrics(db, seeded.tenant_a_id, days=30)
    assert out["eval_runs"] == 1 and out["eval_mean_score"] == 1.0
