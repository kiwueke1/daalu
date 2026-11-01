"""MCP server (docs/design/ai-ops-maturity.md §9): the JSON-RPC dispatcher,
the tools' policy and read-only guarantees, the HTTP route and the stdio
bridge."""

from __future__ import annotations

import io
import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select

REASON = "Revision 4 crashes on start; roll back to revision 3."

AUTO_POLICY = """
version: 1
rules:
  - id: dev-rollback
    match: {tools: [rollout_undo], clusters: ["dev-*"]}
    mode: auto
"""

EXTERNAL_AUTO_POLICY = """
version: 1
rules:
  - id: dev-rollback
    match: {tools: [rollout_undo], clusters: ["dev-*"]}
    mode: auto
    allow_external_auto: true
"""

DENY_POLICY = """
version: 1
rules:
  - id: no-rollbacks
    match: {tools: [rollout_undo]}
    mode: deny
"""


# ── helpers ─────────────────────────────────────────────────────────────────


async def _user(db, seeded):
    from daalu_automation.models import User

    return await db.get(User, seeded.user_a_id)


async def _rpc(db, seeded, method: str, params: dict | None = None, *, msg_id: Any = 1):
    from daalu_automation.core.aiops import mcp

    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    return await mcp.handle(msg, db=db, tenant_id=seeded.tenant_a_id, user=await _user(db, seeded))


async def _call(db, seeded, name: str, arguments: dict | None = None) -> dict[str, Any]:
    resp = await _rpc(db, seeded, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "result" in resp, resp
    return resp["result"]


async def _alert(db, seeded, *, cluster: str = "dev-1"):
    from daalu_automation.models import Alert
    from daalu_automation.models.alert import AlertSeverity, AlertStatus

    alert = Alert(
        tenant_id=seeded.tenant_a_id, module="infra", severity=AlertSeverity.warning,
        status=AlertStatus.open, title="DummyAppDown", body="dummy-app down",
        metadata_json={"cluster": cluster, "namespace": "demo"},
    )
    db.add(alert)
    await db.commit()
    return alert


async def _policy(db, seeded, text: str) -> None:
    from daalu_automation.core.aiops import policy as pol

    await pol.save_policy(db, seeded.tenant_a_id, text, user_id=None)
    await db.commit()


@pytest.fixture
def no_preview(monkeypatch):
    from daalu_automation.api.routers import alert_chat

    async def fake_preview(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return {"summary": "preview", "blast_radius": {"pods": 1, "deployments": 1},
                "details": {}}

    monkeypatch.setattr(alert_chat, "_preview_write", fake_preview)


async def _audit_kinds(db, seeded) -> list[tuple[str, str]]:
    from daalu_automation.models import AuditEvent

    rows = (await db.execute(
        select(AuditEvent.kind, AuditEvent.actor)
        .where(AuditEvent.tenant_id == seeded.tenant_a_id)
        .order_by(AuditEvent.seq)
    )).all()
    return [(k, a) for k, a in rows]


# ── protocol ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_initialize_ping_and_errors(db, seeded):
    resp = await _rpc(db, seeded, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    })
    r = resp["result"]
    assert resp["id"] == 1 and resp["jsonrpc"] == "2.0"
    assert r["protocolVersion"] == "2025-06-18"
    assert r["capabilities"] == {"tools": {"listChanged": False}}
    assert r["serverInfo"]["name"] == "daalu"

    # Unknown client version → we answer with ours.
    resp = await _rpc(db, seeded, "initialize", {"protocolVersion": "1999-01-01"})
    assert resp["result"]["protocolVersion"] == "2025-06-18"

    assert (await _rpc(db, seeded, "ping", msg_id="abc")) == {
        "jsonrpc": "2.0", "id": "abc", "result": {}}
    assert await _rpc(db, seeded, "notifications/initialized", msg_id=None) is None
    assert await _rpc(db, seeded, "tools/call", {"name": "list_alerts"}, msg_id=None) is None

    resp = await _rpc(db, seeded, "resources/list", msg_id=7)
    assert resp["error"]["code"] == -32601 and resp["id"] == 7

    from daalu_automation.core.aiops import mcp

    user = await _user(db, seeded)
    bad = await mcp.handle({"jsonrpc": "1.0", "id": 3, "method": "ping"},
                           db=db, tenant_id=seeded.tenant_a_id, user=user)
    assert bad["error"]["code"] == -32600
    assert (await mcp.handle([{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
                             db=db, tenant_id=seeded.tenant_a_id, user=user)
            )["error"]["code"] == -32600
    # A client response to a server request: nothing to answer.
    assert await mcp.handle({"jsonrpc": "2.0", "id": 9, "result": {}},
                            db=db, tenant_id=seeded.tenant_a_id, user=user) is None

    resp = await _rpc(db, seeded, "tools/call", {"name": "no_such_tool", "arguments": {}})
    assert resp["error"]["code"] == -32602


@pytest.mark.asyncio(loop_scope="session")
async def test_tools_list(db, seeded):
    resp = await _rpc(db, seeded, "tools/list", {})
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    for name in ("list_alerts", "get_investigation", "list_clusters", "run_read_tool",
                 "recent_changes", "propose_action", "get_policy", "simulate_policy",
                 "list_runbooks", "get_runbook", "get_maturity", "submit_verdict"):
        assert name in tools, name
        t = tools[name]
        assert t["inputSchema"]["type"] == "object"
        assert {"readOnlyHint", "destructiveHint"} <= set(t["annotations"])
    assert tools["propose_action"]["annotations"]["readOnlyHint"] is False
    assert tools["list_alerts"]["annotations"]["readOnlyHint"] is True
    read_enum = tools["run_read_tool"]["inputSchema"]["properties"]["tool"]["enum"]
    assert "get_pod_logs" in read_enum
    assert "rollout_undo" not in read_enum and "propose_change" not in read_enum


# ── tools ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_list_alerts_and_audit(db, seeded):
    alert = await _alert(db, seeded)
    res = await _call(db, seeded, "list_alerts", {"status": "open"})
    assert res["isError"] is False
    data = res["structuredContent"]
    assert data["count"] == 1 and data["alerts"][0]["id"] == str(alert.id)
    assert data["alerts"][0]["cluster"] == "dev-1"
    assert json.loads(res["content"][0]["text"]) == data

    from daalu_automation.core.aiops import audit
    from daalu_automation.models import AuditEvent

    ev = (await db.execute(select(AuditEvent).where(
        AuditEvent.tenant_id == seeded.tenant_a_id, AuditEvent.kind == "mcp.call"))).scalar_one()
    assert ev.actor == "alice@tenant-a.example" and ev.subject == "list_alerts"
    assert ev.data["tool"] == "list_alerts" and ev.data["arguments"] == {"status": "open"}
    assert (await audit.verify_chain(db, seeded.tenant_a_id))["ok"]


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_arguments_are_truncated(db, seeded):
    await _call(db, seeded, "get_runbook", {"id": "x" * 10_000})
    kinds = await _audit_kinds(db, seeded)
    assert kinds == [("mcp.call", "alice@tenant-a.example")]
    from daalu_automation.models import AuditEvent

    ev = (await db.execute(select(AuditEvent).where(
        AuditEvent.tenant_id == seeded.tenant_a_id))).scalar_one()
    assert ev.data["arguments"]["_truncated"] is True
    assert len(json.dumps(ev.data)) < 4000


@pytest.mark.asyncio(loop_scope="session")
async def test_run_read_tool_is_read_only(db, seeded, monkeypatch):
    from daalu_automation.core import kube_tools

    calls: list[tuple[str, dict]] = []

    async def fake_execute(name: str, tool_input: dict[str, Any], **kw: Any) -> str:
        calls.append((name, tool_input))
        return f"{name} output"

    monkeypatch.setattr(kube_tools, "execute_tool", fake_execute)

    res = await _call(db, seeded, "run_read_tool",
                      {"tool": "list_pods", "input": {"namespace": "demo"}})
    assert res["isError"] is False
    assert res["structuredContent"] == {"tool": "list_pods", "output": "list_pods output"}

    for tool, inp in (
        ("rollout_undo", {"namespace": "demo", "name": "dummy-app"}),
        ("patch_resource", {}),
        ("call_external_api", {"method": "POST", "url": "https://example.com"}),
        ("propose_change", {}),
    ):
        res = await _call(db, seeded, "run_read_tool", {"tool": tool, "input": inp})
        assert res["isError"] is True, tool
        assert "propose_action" in res["content"][0]["text"]
    assert calls == [("list_pods", {"namespace": "demo"})]

    res = await _call(db, seeded, "run_read_tool", {"tool": "nope"})
    assert res["isError"] is True and "unknown tool" in res["content"][0]["text"]

    async def failing(name: str, tool_input: dict[str, Any], **kw: Any) -> str:
        return "error: kube client unavailable — no kubeconfig"

    monkeypatch.setattr(kube_tools, "execute_tool", failing)
    res = await _call(db, seeded, "run_read_tool", {"tool": "list_pods", "input": {}})
    assert res["isError"] is True and "kube client unavailable" in res["content"][0]["text"]


@pytest.mark.asyncio(loop_scope="session")
async def test_recent_changes(db, seeded, monkeypatch):
    from daalu_automation.core import kube_tools

    seen: dict[str, Any] = {}

    async def fake_execute(name: str, tool_input: dict[str, Any], **kw: Any) -> str:
        seen.update(name=name, input=tool_input, kw=kw)
        return "12:00 deploy/dummy-app revision 4: image busybox:1.36"

    monkeypatch.setattr(kube_tools, "execute_tool", fake_execute)
    res = await _call(db, seeded, "recent_changes",
                      {"namespace": "demo", "hours": 6, "cluster": "dev-1"})
    if "recent_changes" in kube_tools.TOOLS:
        assert res["isError"] is False
        assert seen["name"] == "recent_changes"
        assert seen["input"] == {"namespace": "demo", "hours": 6}
        assert seen["kw"]["cluster_name"] == "dev-1"
        assert "revision 4" in res["structuredContent"]["output"]

    # The read tool is absent on an older server → a clean tool error or the
    # direct change_context fallback, never an exception.
    monkeypatch.delitem(kube_tools.TOOLS, "recent_changes", raising=False)
    import daalu_automation.core.aiops as aiops_pkg

    monkeypatch.setattr(aiops_pkg, "change_context", None, raising=False)
    import sys

    monkeypatch.setitem(sys.modules, "daalu_automation.core.aiops.change_context", None)
    res = await _call(db, seeded, "recent_changes", {"namespace": "demo"})
    assert res["isError"] is True and "not available" in res["content"][0]["text"]


@pytest.mark.asyncio(loop_scope="session")
async def test_propose_action_external_downgrade(db, seeded, no_preview):
    from daalu_automation.models import AlertAction, AlertChatMessage, Investigation
    from daalu_automation.models.alert_chat import ActionStatus, ChatRole

    await _policy(db, seeded, AUTO_POLICY)
    alert = await _alert(db, seeded)

    # Simulating the same write as an MCP caller already shows the downgrade.
    sim = await _call(db, seeded, "simulate_policy",
                      {"tool": "rollout_undo", "input": {"namespace": "demo"},
                       "cluster": "dev-1"})
    assert sim["structuredContent"]["mode"] == "approve"
    assert sim["structuredContent"]["configured_mode"] == "auto"
    sim = await _call(db, seeded, "simulate_policy",
                      {"tool": "rollout_undo", "cluster": "dev-1", "external": False})
    assert sim["structuredContent"]["mode"] == "auto"

    res = await _call(db, seeded, "propose_action", {
        "alert_id": str(alert.id), "tool": "rollout_undo",
        "input": {"namespace": "demo", "name": "dummy-app"}, "reason": REASON,
    })
    assert res["isError"] is False, res
    out = res["structuredContent"]
    assert out["status"] == "pending" and out["auto_executed"] is False
    assert out["policy"]["mode"] == "approve" and out["policy"]["rule_id"] == "dev-rollback"
    assert "allow_external_auto" in out["policy"]["reason"]

    alert_id = alert.id
    db.expire_all()
    action = (await db.execute(select(AlertAction).where(
        AlertAction.alert_id == alert_id))).scalar_one()
    assert action.status is ActionStatus.pending and action.requires_approval
    assert action.actor == "mcp:alice@tenant-a.example"
    assert action.policy_mode == "approve" and action.tool_input["reason"] == REASON
    assert action.preview["summary"] == "preview"
    msg = await db.get(AlertChatMessage, action.message_id)
    assert msg.role is ChatRole.assistant
    assert msg.tool_calls_json[0]["id"] == action.tool_call_id
    assert msg.tool_calls_json[0]["name"] == "rollout_undo"
    assert "alice@tenant-a.example" in msg.content
    inv = (await db.execute(select(Investigation).where(
        Investigation.alert_id == alert_id))).scalar_one()
    assert inv.actions_proposed == 1 and inv.actions_auto == 0

    kinds = await _audit_kinds(db, seeded)
    assert ("action.proposed", "mcp:alice@tenant-a.example") in kinds
    assert kinds.count(("mcp.call", "alice@tenant-a.example")) == 3

    # get_investigation now sees the proposal.
    res = await _call(db, seeded, "get_investigation", {"alert_id": str(alert_id)})
    inv_out = res["structuredContent"]["investigation"]
    assert inv_out["actions"][0]["actor"] == "mcp:alice@tenant-a.example"
    assert res["structuredContent"]["alert"]["title"] == "DummyAppDown"


@pytest.mark.asyncio(loop_scope="session")
async def test_propose_action_validation(db, seeded, no_preview):
    alert = await _alert(db, seeded)
    res = await _call(db, seeded, "propose_action", {
        "alert_id": str(alert.id), "tool": "list_pods", "input": {}, "reason": REASON})
    assert res["isError"] and "run_read_tool" in res["content"][0]["text"]
    res = await _call(db, seeded, "propose_action", {
        "alert_id": str(alert.id), "tool": "scale_deployment",
        "input": {"namespace": "demo", "name": "x", "replicas": "three"}, "reason": REASON})
    assert res["isError"] and "NOT queued" in res["content"][0]["text"]
    res = await _call(db, seeded, "propose_action", {
        "alert_id": "00000000-0000-0000-0000-000000000000", "tool": "rollout_undo",
        "input": {}, "reason": REASON})
    assert res["isError"] and "not found" in res["content"][0]["text"]


@pytest.mark.asyncio(loop_scope="session")
async def test_propose_action_deny(db, seeded, no_preview):
    from daalu_automation.models import AlertAction

    await _policy(db, seeded, DENY_POLICY)
    alert = await _alert(db, seeded)
    res = await _call(db, seeded, "propose_action", {
        "alert_id": str(alert.id), "tool": "rollout_undo",
        "input": {"namespace": "demo", "name": "dummy-app"}, "reason": REASON,
    })
    assert res["isError"] is True
    assert "denied by the autonomy policy" in res["content"][0]["text"]
    assert (await db.execute(select(AlertAction).where(
        AlertAction.alert_id == alert.id))).first() is None
    kinds = await _audit_kinds(db, seeded)
    assert ("action.denied", "mcp:alice@tenant-a.example") in kinds
    assert kinds[-1] == ("mcp.call", "alice@tenant-a.example")


@pytest.mark.asyncio(loop_scope="session")
async def test_propose_action_external_auto_runs(db, seeded, monkeypatch, no_preview):
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.core import kube_tools
    from daalu_automation.models import AlertAction
    from daalu_automation.models.alert_chat import ActionStatus

    async def fake_execute(name: str, tool_input: dict[str, Any], **_: Any) -> str:
        if name == "get_deployment":
            return "deployment demo/dummy-app desired=1 ready=1 available=1"
        return f"{name} ok"

    async def fake_wait(**_: Any) -> str:
        return "deployment demo/dummy-app desired=1 ready=1 available=1"

    monkeypatch.setattr(kube_tools, "execute_tool", fake_execute)
    monkeypatch.setattr(alert_chat, "_wait_for_deployment", fake_wait)

    await _policy(db, seeded, EXTERNAL_AUTO_POLICY)
    alert = await _alert(db, seeded)
    res = await _call(db, seeded, "propose_action", {
        "alert_id": str(alert.id), "tool": "rollout_undo",
        "input": {"namespace": "demo", "name": "dummy-app"}, "reason": REASON,
    })
    assert res["isError"] is False, res
    assert res["structuredContent"]["policy"]["mode"] == "auto"
    assert res["structuredContent"]["auto_executed"] is True
    await alert_chat.drain_auto_tasks()

    alert_id = alert.id
    db.expire_all()
    action = (await db.execute(select(AlertAction).where(
        AlertAction.alert_id == alert_id))).scalar_one()
    assert action.auto_executed and action.status is ActionStatus.executed
    assert action.verified_resolved is True


@pytest.mark.asyncio(loop_scope="session")
async def test_policy_verdict_runbooks_maturity_clusters(db, seeded):
    from daalu_automation.core.aiops import investigations as inv_mod

    res = await _call(db, seeded, "get_policy")
    assert res["isError"] is False
    assert res["structuredContent"]["version"] == 0  # built-in default
    assert "json_schema" not in res["structuredContent"]

    alert = await _alert(db, seeded)
    res = await _call(db, seeded, "get_investigation", {"alert_id": str(alert.id)})
    assert res["isError"] and "no AI investigation" in res["content"][0]["text"]
    res = await _call(db, seeded, "submit_verdict", {"alert_id": str(alert.id),
                                                     "verdict": "correct"})
    assert res["isError"]

    await inv_mod.ensure(db, tenant_id=seeded.tenant_a_id, alert_id=alert.id)
    await db.commit()
    res = await _call(db, seeded, "submit_verdict", {
        "alert_id": str(alert.id), "verdict": "partial", "note": "close"})
    assert res["isError"] is False, res
    out = res["structuredContent"]["investigation"]
    assert out["verdict"] == "partial" and out["verdict_by"] == "alice@tenant-a.example"
    res = await _call(db, seeded, "submit_verdict", {"alert_id": str(alert.id),
                                                     "verdict": "great"})
    assert res["isError"]

    res = await _call(db, seeded, "list_clusters")
    assert res["structuredContent"]["clusters"] == []

    res = await _call(db, seeded, "list_runbooks")
    assert res["isError"] is False and res["structuredContent"]["runbooks"] == []
    res = await _call(db, seeded, "get_runbook", {"id": "missing"})
    assert res["isError"] and "not found" in res["content"][0]["text"]

    # maturity.compute is built in parallel: either it answers, or the tool
    # degrades to a clean error — never a JSON-RPC failure.
    res = await _call(db, seeded, "get_maturity", {"days": 7})
    if res["isError"]:
        assert "not available" in res["content"][0]["text"] or "error" in \
            res["content"][0]["text"]
    else:
        assert "level" in res["structuredContent"]


@pytest.mark.asyncio(loop_scope="session")
async def test_tenant_isolation(db, seeded):
    alert = await _alert(db, seeded)
    from daalu_automation.core.aiops import mcp
    from daalu_automation.models import User

    bob = await db.get(User, seeded.user_b_id)
    resp = await mcp.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "list_alerts", "arguments": {}}},
        db=db, tenant_id=seeded.tenant_b_id, user=bob,
    )
    assert resp["result"]["structuredContent"]["count"] == 0
    resp = await mcp.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "propose_action", "arguments": {
             "alert_id": str(alert.id), "tool": "rollout_undo",
             "input": {"namespace": "demo", "name": "x"}, "reason": REASON}}},
        db=db, tenant_id=seeded.tenant_b_id, user=bob,
    )
    assert resp["result"]["isError"] and "not found" in resp["result"]["content"][0]["text"]


# ── HTTP transport ──────────────────────────────────────────────────────────


async def _pat(db, seeded) -> str:
    from daalu_automation.core.auth import generate_pat, hash_pat
    from daalu_automation.models import PersonalAccessToken

    token = generate_pat()
    db.add(PersonalAccessToken(
        tenant_id=seeded.tenant_a_id, user_id=seeded.user_a_id, name="mcp",
        token_hash=hash_pat(token), prefix=token[:12],
    ))
    await db.commit()
    return token


@pytest.mark.asyncio(loop_scope="session")
async def test_http_route(db, seeded, client):
    token = await _pat(db, seeded)
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}

    r = await client.post("/api/v1/mcp", headers=h, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json()["result"]["protocolVersion"] == "2025-06-18"

    h2 = {**h, "MCP-Protocol-Version": "2025-06-18"}
    r = await client.post("/api/v1/mcp", headers=h2,
                          json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""

    r = await client.post("/api/v1/mcp", headers=h2, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "list_alerts", "arguments": {}}})
    assert r.status_code == 200 and r.json()["result"]["isError"] is False

    r = await client.post("/api/v1/mcp", headers=h2, content=b"{not json")
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    r = await client.post("/api/v1/mcp", headers=h2, json=[{"jsonrpc": "2.0", "id": 1,
                                                           "method": "ping"}])
    assert r.status_code == 400 and r.json()["error"]["code"] == -32600
    r = await client.post("/api/v1/mcp", headers={**h, "MCP-Protocol-Version": "1999-01-01"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 400

    r = await client.get("/api/v1/mcp", headers=h)
    assert r.status_code == 405

    r = await client.post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    r = await client.post("/api/v1/mcp", headers={"Authorization": "Bearer dpat_wrong"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401

    # The session JWT works too (same auth as every route).
    r = await client.post("/api/v1/mcp", headers={"Authorization": f"Bearer {seeded.token_a}"},
                          json={"jsonrpc": "2.0", "id": 5, "method": "ping"})
    assert r.status_code == 200 and r.json() == {"jsonrpc": "2.0", "id": 5, "result": {}}


# ── stdio bridge ────────────────────────────────────────────────────────────


def _bridge(handler):
    from daalu_automation.core.aiops.mcp_cli import Bridge

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Bridge(client, "http://daalu.test/api/v1/mcp", "dpat_test")


def _serve(bridge, lines: list[str]) -> list[dict[str, Any]]:
    from daalu_automation.core.aiops.mcp_cli import serve

    out = io.StringIO()
    serve(io.StringIO("".join(line + "\n" for line in lines)), out, bridge)
    text = out.getvalue()
    assert text == "" or text.endswith("\n")
    return [json.loads(x) for x in text.splitlines()]


def test_stdio_bridge_round_trip():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        msg = json.loads(req.content)
        if "id" not in msg:
            return httpx.Response(202)
        if msg["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "daalu", "version": "0.1.0"}}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"],
                                         "result": {"tools": []}})

    out = _serve(_bridge(handler), [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "",
        json.dumps({"jsonrpc": "2.0", "id": "two", "method": "tools/list"}),
        "{garbage",
    ])
    assert [m.get("id") for m in out] == [1, "two", None]
    assert out[0]["result"]["protocolVersion"] == "2025-06-18"
    assert out[2]["error"]["code"] == -32700
    assert len(seen) == 3
    assert all(r.headers["authorization"] == "Bearer dpat_test" for r in seen)
    assert str(seen[0].url) == "http://daalu.test/api/v1/mcp"
    assert "mcp-protocol-version" not in seen[0].headers
    assert seen[2].headers["mcp-protocol-version"] == "2025-06-18"


def test_stdio_bridge_errors():
    def unauthorized(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid token"})

    out = _serve(_bridge(unauthorized), [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    ])
    assert len(out) == 1 and out[0]["id"] == 1
    assert "HTTP 401" in out[0]["error"]["message"]
    assert "invalid token" in out[0]["error"]["message"]

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=req)

    out = _serve(_bridge(down), [json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})])
    assert out[0]["id"] == 9 and "cannot reach Daalu" in out[0]["error"]["message"]

    def rpc_400(req: httpx.Request) -> httpx.Response:  # JSON-RPC error bodies pass through
        return httpx.Response(400, json={"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32600, "message": "bad"}})

    out = _serve(_bridge(rpc_400), [json.dumps({"jsonrpc": "2.0", "id": 3, "method": "x"})])
    assert out[0]["error"]["code"] == -32600

    def html_500(req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    out = _serve(_bridge(html_500), [json.dumps({"jsonrpc": "2.0", "id": 4, "method": "x"})])
    assert out[0]["id"] == 4 and "HTTP 502" in out[0]["error"]["message"]


def test_endpoint_for():
    from daalu_automation.core.aiops.mcp_cli import endpoint_for

    assert endpoint_for("https://daalu.example.com") == "https://daalu.example.com/api/v1/mcp"
    assert endpoint_for("https://d.example/api/v1/") == "https://d.example/api/v1/mcp"
    assert endpoint_for("https://d.example/api/v1/mcp") == "https://d.example/api/v1/mcp"
