"""Approve & run returns at once; the action runs as a background task.

The approve request used to execute the write *and* wait for the rollout to
converge (a minute or more) before responding, so the page sat frozen with no
feedback. Now it creates the workflow run and returns its id immediately; the
execution fills in the run's steps afterwards.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import BackgroundTasks

pytestmark = pytest.mark.usefixtures("no_aiops_side_calls")


@pytest.mark.asyncio(loop_scope="session")
async def test_approve_returns_running_then_background_finishes(db, seeded, monkeypatch):
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.core import kube_tools
    from daalu_automation.models import Alert, User, WorkflowRun, WorkflowRunStatus
    from daalu_automation.models.alert import AlertSeverity, AlertStatus
    from daalu_automation.models.alert_chat import (
        ActionStatus,
        AlertAction,
        AlertChatMessage,
        ChatRole,
    )

    calls: list[str] = []

    async def fake_execute(name: str, tool_input: dict[str, Any], **_: Any) -> str:
        calls.append(name)
        if name == "get_deployment":
            return "deployment llm-serving/chat-llm desired=1 ready=1 available=1"
        return f"{name} ok"

    monkeypatch.setattr(kube_tools, "execute_tool", fake_execute)

    # While the rollout wait is in progress, step 1 must already be saved,
    # as the run page (a separate session, polling) would see it.
    seen_during_wait: list[int] = []

    async def fake_wait(**_: Any) -> str:
        from daalu_automation.database import AsyncSessionLocal

        async with AsyncSessionLocal() as other:
            r = await other.get(WorkflowRun, run_ids[0])
            seen_during_wait.append(len(r.steps or []))
        return "deployment llm-serving/chat-llm desired=1 ready=1 available=1"

    monkeypatch.setattr(alert_chat, "_wait_for_deployment", fake_wait)
    run_ids: list[Any] = []

    alert = Alert(
        tenant_id=seeded.tenant_a_id,
        module="infra",
        severity=AlertSeverity.critical,
        status=AlertStatus.open,
        title="LLMServerCrashLooping",
        body="",
    )
    db.add(alert)
    await db.flush()
    msg = AlertChatMessage(
        tenant_id=seeded.tenant_a_id, alert_id=alert.id, role=ChatRole.assistant, content=""
    )
    db.add(msg)
    await db.flush()
    action = AlertAction(
        tenant_id=seeded.tenant_a_id,
        alert_id=alert.id,
        message_id=msg.id,
        tool_call_id="t1",
        tool_name="rollout_undo",
        tool_input={"namespace": "llm-serving", "name": "chat-llm", "reason": "roll back"},
        requires_approval=True,
        status=ActionStatus.pending,
    )
    db.add(action)
    await db.commit()
    action_id = action.id
    user = await db.get(User, seeded.user_a_id)

    tasks = BackgroundTasks()
    out = await alert_chat.approve_action(
        alert_id=str(alert.id),
        action_id=str(action.id),
        background_tasks=tasks,
        db=db,
        tenant_id=seeded.tenant_a_id,
        user=user,
    )

    # Returned before anything ran: a running run, no steps, no tool calls.
    run = await db.get(WorkflowRun, out.workflow_run_id)
    run_ids.append(run.id)
    assert run.status is WorkflowRunStatus.running
    assert run.steps == []
    assert calls == []
    assert len(tasks.tasks) == 1

    await tasks()  # what Starlette does after sending the response

    db.expire_all()
    run = await db.get(WorkflowRun, out.workflow_run_id)
    action = await db.get(AlertAction, action_id)
    assert calls[0] == "rollout_undo"
    assert run.status is WorkflowRunStatus.succeeded
    assert run.steps[0]["tool"] == "rollout_undo" and run.steps[0]["status"] == "ok"
    assert [st["order"] for st in run.steps] == list(range(1, len(run.steps) + 1))
    assert len(run.steps) >= 2  # verification recorded
    assert seen_during_wait == [1]  # step 1 was visible before verification ended
    assert action.status is ActionStatus.executed
