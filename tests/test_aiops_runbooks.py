"""Runbooks as code (AOMM §5): parsing, matching, storage, the draft hook,
the HTTP API, injection into triage, and the CLI lint."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from tests.conftest import auth_headers

REPO = Path(__file__).resolve().parents[1]

GOOD = """---
id: dummy-app-down
title: dummy-app is down
match:
  alertname: [DummyAppDown]
  labels: {namespace: daalu-demo}
allowed_actions: [rollout_undo]
---
## Symptoms
down
## Diagnosis
check rollout history
## Fix
kubectl -n daalu-demo rollout undo deploy/dummy-app
"""


def _alert(title="DummyAppDown", **md):
    return SimpleNamespace(title=title, metadata_json=md)


# ── Parsing ──────────────────────────────────────────────────────────────────


def test_parse_good_runbook():
    from daalu_automation.core.aiops import runbooks as rbk

    rb = rbk.parse_markdown(GOOD)
    assert rb.slug == "dummy-app-down"
    assert rb.match == {"alertname": ["DummyAppDown"], "labels": {"namespace": "daalu-demo"}}
    assert rb.allowed_actions == ["rollout_undo"]
    assert rb.body.startswith("## Symptoms")
    assert rbk.lint_markdown(GOOD) == []


@pytest.mark.parametrize(
    "text, needle",
    [
        ("no frontmatter", "frontmatter"),
        ("---\nid: X Bad\ntitle: t\nmatch: {alertname: [A]}\n---\nbody", "`id`"),
        ("---\nid: a\nmatch: {alertname: [A]}\n---\nbody", "`title`"),
        ("---\nid: a\ntitle: t\nmatch: {}\n---\nbody", "at least one"),
        ("---\nid: a\ntitle: t\nmatch: {alertname: [A]}\nallowed_actions: [rm_rf]\n---\nbody", "unknown tools"),
        ("---\nid: a\ntitle: t\nmatch: {alertname: [A]}\ntypo: 1\n---\nbody", "unknown frontmatter"),
        ("---\nid: a\ntitle: t\nmatch: {alertname: [A]}\n---\n   ", "body is empty"),
        ("---\nid: [\n---\nbody", "YAML"),
    ],
)
def test_parse_rejects_bad_runbooks(text, needle):
    from daalu_automation.core.aiops import runbooks as rbk

    with pytest.raises(rbk.RunbookError) as ei:
        rbk.parse_markdown(text)
    assert any(needle in e for e in ei.value.errors), ei.value.errors


def test_lint_warns_on_missing_sections():
    from daalu_automation.core.aiops import runbooks as rbk

    text = "---\nid: a\ntitle: t\nmatch: {alertname: A}\n---\njust prose\n"
    problems = rbk.lint_markdown(text)
    assert problems and all(p.startswith("warning:") for p in problems)


def test_round_trip_to_markdown():
    from daalu_automation.core.aiops import runbooks as rbk

    rb = rbk.parse_markdown(GOOD)
    again = rbk.parse_markdown(rbk.to_markdown(rb))
    assert (again.slug, again.match, again.allowed_actions, again.body) == (
        rb.slug, rb.match, rb.allowed_actions, rb.body,
    )


def test_repo_example_runbooks_are_valid():
    from daalu_automation.core.aiops import runbooks as rbk

    results = rbk.import_dir(REPO / "runbooks")
    assert len(results) >= 2
    for r in results:
        assert r["errors"] == [], (r["path"], r["errors"])
        assert rbk.lint_markdown(r["markdown"]) == [], r["path"]


def test_cli_lint(tmp_path):
    from typer.testing import CliRunner

    from daalu_automation.core.aiops.runbooks_cli import app

    (tmp_path / "ok.md").write_text(GOOD)
    res = CliRunner().invoke(app, ["lint", str(tmp_path)])
    assert res.exit_code == 0, res.output
    (tmp_path / "bad.md").write_text("---\nid: b\n---\nx")
    res = CliRunner().invoke(app, ["lint", str(tmp_path)])
    assert res.exit_code == 1 and "FAIL" in res.output


# ── Matching ─────────────────────────────────────────────────────────────────


def test_matching_rules():
    from daalu_automation.core.aiops import runbooks as rbk

    m = {"alertname": ["DummyAppDown"], "labels": {"namespace": "daalu-demo"}}
    # alertname label wins over title
    a = _alert("something else", labels={"alertname": "DummyAppDown", "namespace": "daalu-demo"})
    assert rbk.matches(m, name=rbk.alert_name(a), labels=rbk.alert_labels(a))
    # alert_name metadata, top-level namespace counts as a label
    a = _alert("x", alert_name="DummyAppDown", namespace="daalu-demo")
    assert rbk.matches(m, name=rbk.alert_name(a), labels=rbk.alert_labels(a))
    # title fallback, case-insensitive
    a = _alert("dummyappdown", namespace="daalu-demo")
    assert rbk.matches(m, name=rbk.alert_name(a), labels=rbk.alert_labels(a))
    # label mismatch / missing
    a = _alert("DummyAppDown", namespace="prod")
    assert not rbk.matches(m, name=rbk.alert_name(a), labels=rbk.alert_labels(a))
    a = _alert("DummyAppDown")
    assert not rbk.matches(m, name=rbk.alert_name(a), labels=rbk.alert_labels(a))
    # wrong name
    a = _alert("HighErrorRate", namespace="daalu-demo")
    assert not rbk.matches(m, name=rbk.alert_name(a), labels=rbk.alert_labels(a))
    # empty match never matches
    assert not rbk.matches({}, name="DummyAppDown", labels={})


def test_render_for_prompt_is_compact():
    from daalu_automation.core.aiops import runbooks as rbk

    rbs = [
        SimpleNamespace(slug=f"r{i}", version=2, title="T", allowed_actions=["rollout_undo"],
                        body="x" * 10_000)
        for i in range(5)
    ]
    text = rbk.render_for_prompt(rbs)
    assert text.count("### Runbook") == 3
    assert "truncated" in text and len(text) < 12_000
    assert rbk.render_for_prompt([]) == ""


# ── Storage + draft hook (DB) ────────────────────────────────────────────────


async def _store(db, tenant_id, markdown=GOOD, *, published=True):
    from daalu_automation.core.aiops import runbooks as rbk

    rb, created, _ = await rbk.upsert_from_markdown(db, tenant_id, markdown)
    if published:
        rbk.publish(rb, by="test")
    await db.commit()
    return rb


@pytest.mark.asyncio(loop_scope="session")
async def test_upsert_bumps_version_and_match_for_alert(db, seeded):
    from daalu_automation.core.aiops import runbooks as rbk
    from daalu_automation.models import Alert
    from daalu_automation.models.alert import AlertSeverity, AlertStatus

    rb = await _store(db, seeded.tenant_a_id, published=False)
    assert rb.version == 1 and rb.source == "imported" and rb.status == "draft"
    same, created, _ = await rbk.upsert_from_markdown(db, seeded.tenant_a_id, GOOD)
    assert not created and same.id == rb.id and same.version == 1  # unchanged
    same, _, _ = await rbk.upsert_from_markdown(db, seeded.tenant_a_id, GOOD + "\nmore\n")
    assert same.version == 2
    await db.commit()

    alert = Alert(
        tenant_id=seeded.tenant_a_id, module="infra", severity=AlertSeverity.warning,
        status=AlertStatus.open, title="DummyAppDown", body="",
        metadata_json={"labels": {"alertname": "DummyAppDown", "namespace": "daalu-demo"}},
    )
    db.add(alert)
    await db.commit()
    assert await rbk.match_for_alert(db, seeded.tenant_a_id, alert) == []  # draft only
    rbk.publish(same, by="test")
    await db.commit()
    assert [r.id for r in await rbk.match_for_alert(db, seeded.tenant_a_id, alert)] == [rb.id]
    # other tenant sees nothing
    assert await rbk.match_for_alert(db, seeded.tenant_b_id, alert) == []
    assert (await rbk.get_runbook(db, seeded.tenant_a_id, str(rb.id))).slug == "dummy-app-down"
    assert (await rbk.get_runbook(db, seeded.tenant_a_id, "dummy-app-down")).id == rb.id
    assert await rbk.get_runbook(db, seeded.tenant_b_id, "dummy-app-down") is None


async def _resolved_alert(db, seeded, *, title="DummyAppDown"):
    from daalu_automation.core.aiops import investigations as inv_mod
    from daalu_automation.models import Alert, AlertAction
    from daalu_automation.models.alert import AlertSeverity, AlertStatus
    from daalu_automation.models.alert_chat import ActionStatus, AlertChatMessage, ChatRole

    alert = Alert(
        tenant_id=seeded.tenant_a_id, module="infra", severity=AlertSeverity.warning,
        status=AlertStatus.open, title=title, body="dummy-app down",
        metadata_json={"labels": {"alertname": title, "namespace": "daalu-demo"}},
    )
    db.add(alert)
    await db.flush()
    msg = AlertChatMessage(tenant_id=seeded.tenant_a_id, alert_id=alert.id,
                           role=ChatRole.assistant, content="")
    db.add(msg)
    await db.flush()
    db.add(AlertAction(
        tenant_id=seeded.tenant_a_id, alert_id=alert.id, message_id=msg.id,
        tool_call_id="w1", tool_name="rollout_undo",
        tool_input={"namespace": "daalu-demo", "name": "dummy-app", "reason": "r"},
        requires_approval=True, status=ActionStatus.executed, verified_resolved=True,
    ))
    inv = await inv_mod.ensure(db, tenant_id=seeded.tenant_a_id, alert_id=alert.id)
    inv.root_cause = "Revision 4 set image nginx:this-tag-does-not-exist."
    await db.commit()
    return alert, inv


def _fake_llm(monkeypatch, calls: list, reply: Any = None, exc: Exception | None = None):
    from daalu_automation.core import llm

    async def fake_complete_json(**kw):
        calls.append(kw)
        if exc is not None:
            raise exc
        return reply if reply is not None else {
            "title": "dummy-app down after a bad rollout",
            "body": "## Symptoms\nx\n## Diagnosis\ny\n## Fix\n1. rollout undo",
            "allowed_actions": ["rollout_undo", "not_a_tool"],
        }

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)


@pytest.mark.asyncio(loop_scope="session")
async def test_draft_from_investigation(db, seeded, monkeypatch):
    from daalu_automation.core.aiops import audit
    from daalu_automation.core.aiops import runbooks as rbk
    from daalu_automation.models import AuditEvent, Runbook

    alert, inv = await _resolved_alert(db, seeded)
    calls: list = []
    _fake_llm(monkeypatch, calls)
    rb = await rbk.maybe_draft_from_investigation(
        db, tenant_id=seeded.tenant_a_id, alert=alert, investigation=inv)
    assert rb is not None and len(calls) == 1
    assert calls[0]["source"] == "aiops.runbook.draft"
    assert calls[0]["tenant_id"] == seeded.tenant_a_id
    assert "rollout_undo" in calls[0]["user"] and "this-tag-does-not-exist" in calls[0]["user"]
    assert (rb.status, rb.source, rb.from_investigation_id) == ("draft", "generated", inv.id)
    assert rb.match == {"alertname": ["DummyAppDown"], "labels": {"namespace": "daalu-demo"}}
    assert rb.allowed_actions == ["rollout_undo"]
    ev = (await db.execute(select(AuditEvent).where(AuditEvent.kind == "runbook.drafted"))).scalar_one()
    assert ev.data["runbook_id"] == str(rb.id)
    assert (await audit.verify_chain(db, seeded.tenant_a_id))["ok"]

    # Idempotent per investigation.
    again = await rbk.maybe_draft_from_investigation(
        db, tenant_id=seeded.tenant_a_id, alert=alert, investigation=inv)
    assert again is None and len(calls) == 1
    n = len((await db.execute(select(Runbook))).scalars().all())
    assert n == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_no_draft_when_published_runbook_matches(db, seeded, monkeypatch):
    from daalu_automation.core.aiops import runbooks as rbk

    await _store(db, seeded.tenant_a_id)
    alert, inv = await _resolved_alert(db, seeded)
    calls: list = []
    _fake_llm(monkeypatch, calls)
    assert await rbk.maybe_draft_from_investigation(
        db, tenant_id=seeded.tenant_a_id, alert=alert, investigation=inv) is None
    assert calls == []


@pytest.mark.asyncio(loop_scope="session")
async def test_draft_never_raises(db, seeded, monkeypatch):
    from daalu_automation.core.aiops import runbooks as rbk
    from daalu_automation.core.llm import LLMUnavailable

    alert, inv = await _resolved_alert(db, seeded)
    _fake_llm(monkeypatch, [], exc=LLMUnavailable("no tiers"))
    assert await rbk.maybe_draft_from_investigation(
        db, tenant_id=seeded.tenant_a_id, alert=alert, investigation=inv) is None
    _fake_llm(monkeypatch, [], exc=ValueError("LLM did not return JSON"))
    assert await rbk.maybe_draft_from_investigation(
        db, tenant_id=seeded.tenant_a_id, alert=alert, investigation=inv) is None
    _fake_llm(monkeypatch, [], reply={"title": "t", "body": ""})
    assert await rbk.maybe_draft_from_investigation(
        db, tenant_id=seeded.tenant_a_id, alert=alert, investigation=inv) is None


# ── Injection into triage ────────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_triage_injects_runbook_and_records_id(db, seeded, monkeypatch):
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.models import Alert, Investigation
    from daalu_automation.models.alert import AlertSeverity, AlertStatus

    rb = await _store(db, seeded.tenant_a_id)
    alert = Alert(
        tenant_id=seeded.tenant_a_id, module="infra", severity=AlertSeverity.warning,
        status=AlertStatus.open, title="DummyAppDown", body="",
        metadata_json={"labels": {"alertname": "DummyAppDown", "namespace": "daalu-demo"}},
    )
    db.add(alert)
    await db.commit()

    seen: list[str] = []

    async def fake_ask_model(**kw: Any) -> dict[str, Any]:
        seen.append(kw.get("runbook_context", ""))
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text":
                "## Root cause\nbad image\n\n## Background\n—\n\n## Remediation plan\n1. x"}]}

    monkeypatch.setattr(alert_chat, "_ask_model", fake_ask_model)
    await alert_chat._record_assistant_turn(db=db, tenant_id=seeded.tenant_a_id, alert=alert)
    assert seen and "Runbook dummy-app-down" in seen[0]
    alert_id, rb_id = alert.id, rb.id
    db.expire_all()
    inv = (await db.execute(select(Investigation).where(Investigation.alert_id == alert_id))).scalar_one()
    assert inv.runbook_ids == [str(rb_id)]


@pytest.mark.asyncio(loop_scope="session")
async def test_auto_investigate_is_idempotent(db, seeded, monkeypatch):
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.models import Alert, AlertChatMessage
    from daalu_automation.models.alert import AlertSeverity, AlertStatus

    alert = Alert(
        tenant_id=seeded.tenant_a_id, module="infra", severity=AlertSeverity.warning,
        status=AlertStatus.open, title="DummyAppDown", body="", metadata_json={},
    )
    db.add(alert)
    await db.commit()
    calls = []

    async def fake_ask_model(**kw: Any) -> dict[str, Any]:
        calls.append(1)
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "## Root cause\nx"}]}

    monkeypatch.setattr(alert_chat, "_ask_model", fake_ask_model)
    await alert_chat.auto_investigate(seeded.tenant_a_id, alert.id)
    first = len(calls)
    assert first >= 1
    await alert_chat.auto_investigate(seeded.tenant_a_id, alert.id)
    assert len(calls) == first
    msgs = (await db.execute(select(AlertChatMessage).where(
        AlertChatMessage.alert_id == alert.id))).scalars().all()
    assert sum(1 for m in msgs if m.content == alert_chat.TRIAGE_KICKOFF) == 1
    # Unknown alert: logged, never raised.
    import uuid

    await alert_chat.auto_investigate(seeded.tenant_a_id, uuid.uuid4())


@pytest.mark.asyncio(loop_scope="session")
async def test_resolving_open_investigation_marks_manual(db, seeded, client):
    from daalu_automation.core.aiops import investigations as inv_mod
    from daalu_automation.models import Alert, Investigation
    from daalu_automation.models.alert import AlertSeverity, AlertStatus

    alert = Alert(
        tenant_id=seeded.tenant_a_id, module="infra", severity=AlertSeverity.warning,
        status=AlertStatus.open, title="X", body="", metadata_json={},
    )
    db.add(alert)
    await db.flush()
    await inv_mod.ensure(db, tenant_id=seeded.tenant_a_id, alert_id=alert.id)
    await db.commit()
    alert_id = alert.id
    r = await client.post(f"/api/v1/alerts/{alert_id}/resolve", headers=auth_headers(seeded.token_a))
    assert r.status_code == 200, r.text
    db.expire_all()
    inv = (await db.execute(select(Investigation).where(Investigation.alert_id == alert_id))).scalar_one()
    assert inv.resolution == "manual" and inv.resolved_at is not None


# ── HTTP API ─────────────────────────────────────────────────────────────────


async def _non_admin_token(db, seeded) -> str:
    from daalu_automation.core.auth import issue_token
    from daalu_automation.models import User

    u = User(tenant_id=seeded.tenant_a_id, email="viewer@a.test", is_active=True,
             is_admin=False, is_superuser=False)
    db.add(u)
    await db.commit()
    token, _ = issue_token(user_id=str(u.id), tenant_id=str(seeded.tenant_a_id), is_admin=False)
    return token


@pytest.mark.asyncio(loop_scope="session")
async def test_runbook_api_lifecycle(db, seeded, client):
    from daalu_automation.models import AuditEvent

    h = auth_headers(seeded.token_a)
    viewer = auth_headers(await _non_admin_token(db, seeded))

    # Create from markdown (as `daalu runbooks import` does).
    r = await client.post("/api/v1/aiops/runbooks", json={"markdown": GOOD}, headers=viewer)
    assert r.status_code == 201, r.text
    rb = r.json()
    assert set(rb) >= {"id", "slug", "title", "body", "match", "allowed_actions", "status",
                       "source", "version", "from_investigation_id", "published_by",
                       "published_at", "created_at", "updated_at"}
    assert (rb["status"], rb["source"], rb["version"]) == ("draft", "imported", 1)
    # Re-import updates by slug.
    r = await client.post("/api/v1/aiops/runbooks", json={"markdown": GOOD + "\nv2\n"}, headers=viewer)
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["id"] == rb["id"]

    # Structured create; duplicate slug → 409; invalid → 422.
    body = {"title": "High errors", "body": "## Fix\nundo", "slug": "high-errors",
            "match": {"alertname": ["HighErrorRate"]}, "allowed_actions": ["rollout_undo"]}
    r = await client.post("/api/v1/aiops/runbooks", json=body, headers=viewer)
    assert r.status_code == 201 and r.json()["source"] == "manual"
    assert (await client.post("/api/v1/aiops/runbooks", json=body, headers=viewer)).status_code == 409
    bad = {**body, "slug": "x", "match": {}}
    r = await client.post("/api/v1/aiops/runbooks", json=bad, headers=viewer)
    assert r.status_code == 422 and "errors" in r.json()["detail"]

    # Publish: admin only, audited.
    r = await client.post(f"/api/v1/aiops/runbooks/{rb['id']}/publish", headers=viewer)
    assert r.status_code == 403
    r = await client.post(f"/api/v1/aiops/runbooks/{rb['id']}/publish", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "published"
    assert r.json()["published_by"] == "alice@tenant-a.example"
    ev = (await db.execute(select(AuditEvent).where(AuditEvent.kind == "runbook.published"))).scalars().all()
    assert len(ev) == 1 and ev[0].data["runbook_id"] == rb["id"]

    # Edit: non-admin can't touch a published runbook; admin edit bumps version.
    r = await client.put(f"/api/v1/aiops/runbooks/{rb['id']}", json={"title": "new"}, headers=viewer)
    assert r.status_code == 403
    r = await client.put("/api/v1/aiops/runbooks/dummy-app-down", json={"title": "new"}, headers=h)
    assert r.status_code == 200 and r.json()["title"] == "new" and r.json()["version"] == 3
    assert r.json()["status"] == "published"

    # List with filter; get by id or slug.
    r = await client.get("/api/v1/aiops/runbooks?status=published", headers=viewer)
    assert [x["slug"] for x in r.json()] == ["dummy-app-down"]
    assert len((await client.get("/api/v1/aiops/runbooks", headers=viewer)).json()) == 2
    assert (await client.get(f"/api/v1/aiops/runbooks/{rb['id']}", headers=viewer)).json()["slug"] == "dummy-app-down"
    assert (await client.get("/api/v1/aiops/runbooks/nope", headers=viewer)).status_code == 404
    # Tenant isolation.
    assert (await client.get(f"/api/v1/aiops/runbooks/{rb['id']}", headers=auth_headers(seeded.token_b))).status_code == 404

    # Match endpoint.
    from daalu_automation.models import Alert
    from daalu_automation.models.alert import AlertSeverity, AlertStatus

    alert = Alert(
        tenant_id=seeded.tenant_a_id, module="infra", severity=AlertSeverity.warning,
        status=AlertStatus.open, title="DummyAppDown", body="",
        metadata_json={"labels": {"alertname": "DummyAppDown", "namespace": "daalu-demo"}},
    )
    db.add(alert)
    await db.commit()
    r = await client.get(f"/api/v1/aiops/runbooks/match?alert_id={alert.id}", headers=viewer)
    assert r.status_code == 200 and [x["id"] for x in r.json()] == [rb["id"]]

    # Delete: published needs admin; drafts anyone.
    assert (await client.delete(f"/api/v1/aiops/runbooks/{rb['id']}", headers=viewer)).status_code == 403
    assert (await client.delete(f"/api/v1/aiops/runbooks/{rb['id']}", headers=h)).status_code == 204
    assert (await client.delete("/api/v1/aiops/runbooks/high-errors", headers=viewer)).status_code == 204
    assert (await client.get("/api/v1/aiops/runbooks", headers=h)).json() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_agent_host_auto_investigates_only_when_policy_says_so(db, seeded, monkeypatch):
    import asyncio
    import uuid

    from daalu_automation.api.routers import alert_chat
    from daalu_automation.core.aiops import policy as pol
    from daalu_automation.modules.infra import agent as infra_agent

    started: list = []

    async def fake_auto(tenant_id, alert_id):
        started.append((tenant_id, alert_id))

    monkeypatch.setattr(alert_chat, "auto_investigate", fake_auto)
    aid = uuid.uuid4()
    await infra_agent._maybe_auto_investigate(seeded.tenant_a_id, aid)  # default policy: off
    await asyncio.gather(*infra_agent._AUTO_INVESTIGATIONS)
    assert started == []

    await pol.save_policy(db, seeded.tenant_a_id, "investigate_automatically: true\n", user_id=None)
    await db.commit()
    await infra_agent._maybe_auto_investigate(seeded.tenant_a_id, aid)
    await asyncio.gather(*infra_agent._AUTO_INVESTIGATIONS)
    assert started == [(seeded.tenant_a_id, aid)]
