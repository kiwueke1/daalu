"""Malformed write-tool calls never become approve cards; every write says why.

Regression for the S1-01 rehearsal (2026-09-28): Qwen3.6 via vLLM's
``qwen3_coder`` parser proposed ``patch_resource(patch="")`` next to a
``delete_pod``. Both landed as "ready to run" cards, though the patch could only
fail or change nothing. Now a write's arguments are validated before it is
queued. A bad plan goes back to the model as tool errors, and every
approval-gated call must carry a one-line ``reason`` for the operator.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from daalu_automation.core import kube_tools

pytestmark = pytest.mark.usefixtures("no_aiops_side_calls")

REASON = "Roll back to revision 3 (GPU_MEM_UTIL=0.10) so vLLM fits and the pod goes Ready."
EMPTY_PATCH = {
    "namespace": "llm-serving",
    "kind": "Deployment",
    "name": "chat-llm",
    "patch": "",
    "reason": "Lower GPU memory.",
}


# ── validate_write_input ────────────────────────────────────────────────


def test_empty_patch_is_rejected() -> None:
    _, err = kube_tools.validate_write_input("patch_resource", EMPTY_PATCH)
    assert err is not None
    assert "NOT queued" in err
    assert "`patch`" in err


def test_json_string_patch_is_normalized_to_object() -> None:
    raw = {**EMPTY_PATCH, "patch": '{"spec": {"replicas": 1}}'}
    out, err = kube_tools.validate_write_input("patch_resource", raw)
    assert err is None
    assert out["patch"] == {"spec": {"replicas": 1}}


def test_missing_reason_is_rejected() -> None:
    _, err = kube_tools.validate_write_input(
        "rollout_undo", {"namespace": "llm-serving", "name": "chat-llm"}
    )
    assert err is not None and "`reason`" in err


def test_wrong_enum_and_type_are_rejected() -> None:
    _, err = kube_tools.validate_write_input(
        "scale_deployment",
        {"namespace": "a", "name": "b", "replicas": "two", "reason": REASON},
    )
    assert err is not None and "`replicas`" in err
    _, err = kube_tools.validate_write_input(
        "patch_resource",
        {**EMPTY_PATCH, "kind": "Ingress", "patch": {"spec": {}}},
    )
    assert err is not None and "`kind`" in err


def test_valid_rollout_undo_passes() -> None:
    args = {"namespace": "llm-serving", "name": "chat-llm", "reason": REASON}
    out, err = kube_tools.validate_write_input("rollout_undo", args)
    assert err is None and out == args


# ── schema + execution ──────────────────────────────────────────────────


def _params(tool: str) -> dict[str, Any]:
    for d in kube_tools.openai_tool_definitions():
        if d["function"]["name"] == tool:
            return d["function"]["parameters"]
    raise AssertionError(tool)


def test_reason_is_required_on_write_tools_only() -> None:
    for tool in ("patch_resource", "delete_pod", "rollout_undo"):
        assert "reason" in _params(tool)["properties"]
        assert "reason" in _params(tool)["required"]
    assert "reason" not in _params("get_pod_logs").get("properties", {})
    # The registry's own schema is untouched (copied, not mutated).
    assert "reason" not in kube_tools.TOOLS["patch_resource"].input_schema["properties"]


@pytest.mark.asyncio
async def test_execute_tool_strips_reason(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_handler(*, namespace: str, name: str) -> str:
        seen.update(namespace=namespace, name=name)
        return "deleted"

    spec = kube_tools.TOOLS["delete_pod"]
    monkeypatch.setitem(
        kube_tools.TOOLS,
        "delete_pod",
        kube_tools.ToolSpec(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            handler=fake_handler,
            requires_approval=True,
        ),
    )
    out = await kube_tools.execute_tool(
        "delete_pod", {"namespace": "ns", "name": "p", "reason": REASON}
    )
    assert out == "deleted"
    assert seen == {"namespace": "ns", "name": "p"}


# ── the agent loop ──────────────────────────────────────────────────────


def _tool_use(i: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": i, "name": name, "input": args}


@pytest.mark.asyncio(loop_scope="session")
async def test_bad_plan_is_bounced_then_corrected_plan_queued(
    db, seeded, monkeypatch
) -> None:
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.models import Alert
    from daalu_automation.models.alert import AlertSeverity, AlertStatus
    from daalu_automation.models.alert_chat import (
        ActionStatus,
        AlertAction,
        AlertChatMessage,
        ChatRole,
    )

    # Evidence already gathered, so the loop doesn't force a first-turn tool.
    alert = Alert(
        tenant_id=seeded.tenant_a_id,
        module="infra",
        severity=AlertSeverity.warning,
        status=AlertStatus.open,
        title="LLMServerCrashLooping",
        body="chat-llm crash looping",
    )
    db.add(alert)
    await db.flush()
    db.add(
        AlertChatMessage(
            tenant_id=seeded.tenant_a_id,
            alert_id=alert.id,
            role=ChatRole.tool,
            content="revision 4: GPU_MEM_UTIL=0.90",
            tool_call_id="prior",
        )
    )
    await db.commit()

    replies = [
        {  # the rehearsal's plan: empty patch + a pod bounce
            "content": [
                _tool_use("c1", "patch_resource", EMPTY_PATCH),
                _tool_use(
                    "c2",
                    "delete_pod",
                    {"namespace": "llm-serving", "name": "chat-llm-x", "reason": "Bounce."},
                ),
            ],
            "stop_reason": "tool_use",
        },
        {  # corrected after seeing the errors
            "content": [
                {"type": "text", "text": "The history shows revision 3 was healthy; rolling back."},
                _tool_use(
                    "c3",
                    "rollout_undo",
                    {"namespace": "llm-serving", "name": "chat-llm", "reason": REASON},
                ),
            ],
            "stop_reason": "tool_use",
        },
        {  # the write-up it skipped; a repeated tool call must be dropped
            "content": [
                {
                    "type": "text",
                    "text": "## Root cause\nGPU_MEM_UTIL=0.90.\n\n## Background\n—\n\n"
                    "## Remediation plan\n1. **[system]** Roll back.",
                },
                _tool_use(
                    "c4",
                    "rollout_undo",
                    {"namespace": "llm-serving", "name": "chat-llm", "reason": REASON},
                ),
            ],
            "stop_reason": "tool_use",
        },
    ]
    nudges: list[str | None] = []

    async def fake_ask_model(**kw: Any) -> dict[str, Any]:
        nudges.append(kw.get("nudge"))
        return replies.pop(0)

    monkeypatch.setattr(alert_chat, "_ask_model", fake_ask_model)
    await alert_chat._record_assistant_turn(
        db=db, tenant_id=seeded.tenant_a_id, alert=alert
    )

    actions = (
        await db.execute(select(AlertAction).where(AlertAction.alert_id == alert.id))
    ).scalars().all()
    assert [(a.tool_name, a.status) for a in actions] == [
        ("rollout_undo", ActionStatus.pending)
    ]
    assert actions[0].tool_input["reason"] == REASON

    tool_msgs = {
        m.tool_call_id: m.content
        for m in (
            await db.execute(
                select(AlertChatMessage).where(
                    AlertChatMessage.alert_id == alert.id,
                    AlertChatMessage.role == ChatRole.tool,
                )
            )
        ).scalars()
    }
    assert "`patch` is required" in tool_msgs["c1"]
    assert "another action in this plan" in tool_msgs["c2"]
    assert not replies  # plan, corrected plan, write-up
    assert nudges[2] is not None and "queued for operator approval" in nudges[2]
    summaries = (
        await db.execute(
            select(AlertChatMessage).where(
                AlertChatMessage.alert_id == alert.id,
                AlertChatMessage.role == ChatRole.assistant,
                AlertChatMessage.content.contains("## Root cause"),
            )
        )
    ).scalars().all()
    assert len(summaries) == 1 and summaries[0].tool_calls_json == []


# ── rollout_history shows what changed between revisions ────────────────


def _ns(**kw: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


def _rs(rev: str, gpu: str) -> Any:
    env = [
        _ns(name="GPU_MEM_UTIL", value=gpu, value_from=None),
        _ns(name="HF_HOME", value="/cache", value_from=None),
        _ns(name="HF_TOKEN", value=None, value_from=object()),
    ]
    container = _ns(name="vllm", image="vllm/vllm-openai:v0.29.0", env=env, args=None, command=None)
    return _ns(
        metadata=_ns(
            annotations={"deployment.kubernetes.io/revision": rev},
            labels={"pod-template-hash": f"h{rev}"},
            creation_timestamp=f"t{rev}",
        ),
        spec=_ns(template=_ns(spec=_ns(containers=[container]))),
    )


@pytest.mark.asyncio
async def test_rollout_history_shows_env_that_differs(monkeypatch) -> None:
    dep = _ns(
        metadata=_ns(annotations={"deployment.kubernetes.io/revision": "4"}),
        spec=_ns(selector=_ns(match_labels={"app": "chat-llm"})),
    )
    rs_list = _ns(items=[_rs("3", "0.10"), _rs("4", "0.90")])

    async def fake_client(_tenant: Any) -> Any:
        return None, _ns(read_namespaced_deployment=None, list_namespaced_replica_set=None), None

    async def fake_run(fn: Any, **_: Any) -> Any:
        return rs_list if "selector" in str(_) else dep

    monkeypatch.setattr(kube_tools, "_tenant_kube_or_default", fake_client)
    monkeypatch.setattr(kube_tools, "_run", fake_run)
    out = await kube_tools._rollout_history(namespace="llm-serving", name="chat-llm")

    assert "Current revision: 4" in out
    lines = out.splitlines()
    assert lines[2].startswith("4 ") and "vllm env GPU_MEM_UTIL=0.90" in lines[2]
    assert lines[3].startswith("3 ") and "vllm env GPU_MEM_UTIL=0.10" in lines[3]
    assert "HF_HOME" not in out  # unchanged settings stay out


@pytest.mark.asyncio(loop_scope="session")
async def test_summary_requested_when_write_queued_without_one(
    db, seeded, monkeypatch
) -> None:
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.models import Alert
    from daalu_automation.models.alert import AlertSeverity, AlertStatus
    from daalu_automation.models.alert_chat import (
        AlertAction,
        AlertChatMessage,
        ChatRole,
    )

    alert = Alert(
        tenant_id=seeded.tenant_a_id,
        module="infra",
        severity=AlertSeverity.warning,
        status=AlertStatus.open,
        title="LLMServerCrashLooping",
        body="chat-llm crash looping",
    )
    db.add(alert)
    await db.flush()
    db.add(
        AlertChatMessage(
            tenant_id=seeded.tenant_a_id,
            alert_id=alert.id,
            role=ChatRole.tool,
            content="evidence",
            tool_call_id="prior",
        )
    )
    await db.commit()

    undo = {"namespace": "llm-serving", "name": "chat-llm", "reason": REASON}
    replies = [
        {
            "content": [
                {"type": "text", "text": "The evidence is clear; rolling back."},
                _tool_use("u1", "rollout_undo", undo),
            ],
            "stop_reason": "tool_use",
        },
        {  # bare headings, as Qwen writes them
            "content": [
                {
                    "type": "text",
                    "text": "Root cause\nGPU_MEM_UTIL=0.90.\n\nBackground\n—\n\n"
                    "Remediation plan\n1. **[system]** Roll back.",
                }
            ],
            "stop_reason": "end_turn",
        },
    ]

    async def fake_ask_model(**_: Any) -> dict[str, Any]:
        return replies.pop(0)

    monkeypatch.setattr(alert_chat, "_ask_model", fake_ask_model)
    await alert_chat._record_assistant_turn(
        db=db, tenant_id=seeded.tenant_a_id, alert=alert
    )

    assert not replies
    actions = (
        await db.execute(select(AlertAction).where(AlertAction.alert_id == alert.id))
    ).scalars().all()
    assert [a.tool_name for a in actions] == ["rollout_undo"]
