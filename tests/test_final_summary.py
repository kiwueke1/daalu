"""The investigation loop can hit its turn cap right after queuing a fix,
before the three-section write-up. ``_final_summary`` writes it from the
evidence already gathered, without running or queuing any tool."""

import asyncio
import types
import uuid

from daalu_automation.api.routers import alert_chat


class _FakeDB:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _run(monkeypatch, reply):
    calls = {}

    async def fake_load_chat(db, tenant_id, alert_id):
        return [], {}

    async def fake_ask_model(**kw):
        calls.update(kw)
        return reply

    monkeypatch.setattr(alert_chat, "_load_chat", fake_load_chat)
    monkeypatch.setattr(alert_chat, "_ask_model", fake_ask_model)
    db = _FakeDB()
    inv = types.SimpleNamespace(model="", triaged_at=None, root_cause="")
    alert = types.SimpleNamespace(id=uuid.uuid4())
    asyncio.run(alert_chat._final_summary(
        db=db, tenant_id=uuid.uuid4(), alert=alert, investigation=inv, runbook_context="",
    ))
    return db, inv, calls


def test_final_summary_posts_answer_and_drops_tools(monkeypatch):
    text = (
        "## Root cause\nThe readiness probe checks port 65000; glance-api listens on 9292.\n\n"
        "## Background\nRevision 2 changed the pod template.\n\n"
        "## Remediation plan\n1. [system] Patch the probe port to 9292 (queued for approval)."
    )
    reply = {"content": [
        {"type": "text", "text": text},
        {"type": "tool_use", "id": "t1", "name": "patch_resource", "input": {}},
    ], "model": "qwen"}
    db, inv, calls = _run(monkeypatch, reply)

    assert calls["nudge"] == alert_chat._FINAL_SUMMARY_NUDGE
    assert calls["force_tool_use"] is False
    (msg,) = db.added
    assert msg.content == text
    assert msg.tool_calls_json == []  # the stray tool call is not queued
    assert db.commits == 1
    assert "9292" in inv.root_cause and inv.triaged_at is not None


def test_final_summary_skips_empty_reply(monkeypatch):
    db, inv, _ = _run(monkeypatch, {"content": [{"type": "text", "text": "  "}]})
    assert db.added == [] and db.commits == 0
