"""Daalu as a Model Context Protocol server (AOMM "open tool protocol").

Transport-independent JSON-RPC 2.0 dispatcher for MCP protocol version
``2025-06-18``. :func:`handle` takes one decoded JSON-RPC message and returns
the response message, or ``None`` for notifications (and for stray responses).
The HTTP route (``POST /api/v1/mcp``) and the ``daalu mcp`` stdio bridge both
end up here, so the tool surface is identical on every transport.

Design rules (docs/design/ai-ops-maturity.md §9):

* External agents get the same read tools the built-in agent has, but never a
  write: ``run_read_tool`` refuses anything ``kube_tools`` gates behind
  approval. Writes go through ``propose_action``, which attaches the write to
  an alert as an ``AlertAction`` decided by the tenant's autonomy policy with
  ``external=True`` — ``auto`` is downgraded to ``approve`` unless the matching
  rule sets ``allow_external_auto``, and ``deny`` is a tool error.
* Every ``tools/call`` writes an ``mcp.call`` audit event (actor = the
  caller's email; data = tool name + truncated arguments + outcome).
* Tool results carry the JSON both as text content and as
  ``structuredContent``.

The ``mcp`` SDK is deliberately not used: the protocol surface we need is
small and hand-rolling it keeps the dependency set unchanged.
"""

from __future__ import annotations

import enum
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.aiops import audit

logger = structlog.get_logger(__name__)

PROTOCOL_VERSION = "2025-06-18"
# Versions we can speak. The wire format of the methods we implement is the
# same across them; we echo the client's version when we support it.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "daalu"
SERVER_TITLE = "Daalu AI-Ops"
SERVER_VERSION = "0.1.0"

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "Daalu is an AI-Ops platform. Use list_alerts / get_investigation to see "
    "what is broken and what Daalu's own agent concluded; run_read_tool and "
    "recent_changes to gather evidence (read-only). You cannot change "
    "infrastructure directly: propose_action attaches a write to an alert, "
    "where the tenant's autonomy policy decides whether a human must approve "
    "it. get_policy / simulate_policy show what the policy would do."
)

# Tools that kube_tools marks as not needing approval but which still change
# state somewhere (propose_change authors a SoT ChangeProposal). An external
# agent must not reach them through the read-only door.
_NOT_READ_ONLY = frozenset({"propose_change"})

_AUDIT_ARG_LIMIT = 2000


class ToolError(Exception):
    """A tool failed in a way the caller should see (``isError: true``)."""


@dataclass(frozen=True)
class CallContext:
    db: AsyncSession
    tenant_id: uuid.UUID
    user: Any
    email: str

    @property
    def actor(self) -> str:
        return f"mcp:{self.email}"


Handler = Callable[[CallContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    read_only: bool = True
    destructive: bool = False

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.read_only,
                "openWorldHint": False,
            },
        }


# ── JSON helpers ────────────────────────────────────────────────────────────


def _jsonable(obj: Any) -> Any:
    """Turn ORM rows, Pydantic models, datetimes, UUIDs, enums into JSON."""
    if isinstance(obj, enum.Enum):
        return _jsonable(obj.value)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if hasattr(obj, "model_dump"):
        return _jsonable(obj.model_dump(mode="json"))
    if hasattr(obj, "__table__"):  # ORM row
        from sqlalchemy import inspect as sa_inspect

        return {a.key: _jsonable(getattr(obj, a.key, None))
                for a in sa_inspect(obj).mapper.column_attrs}
    return str(obj)


def _truncate(value: Any, limit: int = _AUDIT_ARG_LIMIT) -> Any:
    """Arguments as stored in the audit log: whole if small, else a prefix."""
    text = json.dumps(_jsonable(value), sort_keys=True, default=str)
    if len(text) <= limit:
        return json.loads(text)
    return {"_truncated": True, "_length": len(text), "_preview": text[:limit]}


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    data = _jsonable(data)
    return {
        "content": [{"type": "text", "text": json.dumps(data, indent=2, sort_keys=False)}],
        "structuredContent": data,
        "isError": False,
    }


def _err_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _rpc_result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def rpc_error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _uuid_arg(args: dict[str, Any], key: str) -> uuid.UUID:
    raw = args.get(key)
    if not raw:
        raise ToolError(f"`{key}` is required")
    try:
        return uuid.UUID(str(raw))
    except ValueError as e:
        raise ToolError(f"`{key}` must be a UUID, got {raw!r}") from e


def _http_detail(e: Exception) -> str:
    detail = getattr(e, "detail", None)
    return str(detail) if detail else str(e)


# ── Tools ───────────────────────────────────────────────────────────────────


async def _load_alert(ctx: CallContext, alert_id: uuid.UUID) -> Any:
    from daalu_automation.models import Alert

    alert = (
        await ctx.db.execute(
            select(Alert).where(Alert.id == alert_id, Alert.tenant_id == ctx.tenant_id)
        )
    ).scalar_one_or_none()
    if alert is None:
        raise ToolError(f"alert {alert_id} not found")
    return alert


async def tool_list_alerts(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from daalu_automation.models import Alert, Investigation

    limit = max(1, min(int(args.get("limit") or 50), 200))
    stmt = (
        select(Alert)
        .where(Alert.tenant_id == ctx.tenant_id)
        .order_by(desc(func.coalesce(Alert.last_seen_at, Alert.created_at)))
        .limit(limit)
    )
    for key in ("status", "severity", "module"):
        if args.get(key):
            stmt = stmt.where(getattr(Alert, key) == args[key])
    alerts = (await ctx.db.execute(stmt)).scalars().all()
    invs = {}
    if alerts:
        rows = (
            await ctx.db.execute(
                select(Investigation).where(
                    Investigation.tenant_id == ctx.tenant_id,
                    Investigation.alert_id.in_([a.id for a in alerts]),
                )
            )
        ).scalars().all()
        invs = {i.alert_id: i for i in rows}
    out = []
    for a in alerts:
        md = a.metadata_json or {}
        labels = md.get("labels") if isinstance(md.get("labels"), dict) else {}
        inv = invs.get(a.id)
        out.append({
            "id": a.id,
            "title": a.title,
            "severity": a.severity,
            "status": a.status,
            "module": a.module,
            "created_at": a.created_at,
            "last_seen_at": a.last_seen_at,
            "occurrence_count": a.occurrence_count,
            "cluster": md.get("cluster") or labels.get("cluster"),
            "namespace": md.get("namespace") or labels.get("namespace"),
            "investigation": None if inv is None else {
                "resolution": inv.resolution,
                "diagnosed": inv.diagnosed_at is not None,
                "verdict": inv.verdict,
            },
        })
    return {"alerts": out, "count": len(out)}


async def tool_get_investigation(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from fastapi import HTTPException

    from daalu_automation.api.routers import aiops_investigations as inv_router
    from daalu_automation.models import AlertChatMessage, ChatRole

    alert_id = _uuid_arg(args, "alert_id")
    alert = await _load_alert(ctx, alert_id)
    try:
        detail = await inv_router.get_by_alert(str(alert_id), db=ctx.db, tenant_id=ctx.tenant_id)
    except HTTPException as e:
        if e.status_code == 404:
            raise ToolError(
                f"alert {alert_id} has no AI investigation yet. Open it in Daalu "
                "(or enable investigate_automatically in the policy) to start one."
            ) from e
        raise ToolError(_http_detail(e)) from e
    last = (
        await ctx.db.execute(
            select(AlertChatMessage.content)
            .where(
                AlertChatMessage.tenant_id == ctx.tenant_id,
                AlertChatMessage.alert_id == alert_id,
                AlertChatMessage.role == ChatRole.assistant,
                AlertChatMessage.content != "",
            )
            .order_by(desc(AlertChatMessage.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "investigation": detail,
        "alert": {
            "id": alert.id, "title": alert.title, "body": alert.body[:4000],
            "severity": alert.severity, "status": alert.status,
            "metadata": alert.metadata_json or {},
        },
        "latest_summary": last or "",
    }


async def tool_list_clusters(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from daalu_automation.models import ClusterTunnel, Integration

    tunnels = (
        await ctx.db.execute(
            select(ClusterTunnel)
            .where(ClusterTunnel.tenant_id == ctx.tenant_id)
            .order_by(ClusterTunnel.created_at.desc())
        )
    ).scalars().all()
    kube = (
        await ctx.db.execute(
            select(Integration)
            .where(Integration.tenant_id == ctx.tenant_id, Integration.provider == "kubernetes")
            .order_by(Integration.name)
        )
    ).scalars().all()
    return {
        "clusters": [
            {"slug": t.slug, "name": t.name, "status": t.status,
             "last_handshake_at": t.last_handshake_at}
            for t in tunnels
        ],
        "kubernetes_integrations": [
            {"name": i.name, "status": i.status} for i in kube
        ],
        "hint": "Pass a cluster slug (or kubernetes integration name) as `cluster` "
                "to run_read_tool / recent_changes. Omitted → the tenant's default cluster.",
    }


def _read_tool_names() -> list[str]:
    from daalu_automation.core import kube_tools

    return sorted(
        name for name, spec in kube_tools.TOOLS.items()
        if not spec.requires_approval and name not in _NOT_READ_ONLY
    )


async def _cluster_target(ctx: CallContext, cluster: str | None) -> dict[str, str | None]:
    """A cluster argument may be an onboarded cluster slug or a kubernetes
    integration name; kube_tools takes them on different parameters."""
    if not cluster:
        return {"cluster_name": None, "cluster_slug": None}
    from daalu_automation.models import ClusterTunnel

    slug = (
        await ctx.db.execute(
            select(ClusterTunnel.slug).where(
                ClusterTunnel.tenant_id == ctx.tenant_id, ClusterTunnel.slug == cluster
            )
        )
    ).scalar_one_or_none()
    if slug:
        return {"cluster_name": None, "cluster_slug": slug}
    return {"cluster_name": cluster, "cluster_slug": None}


async def tool_list_read_tools(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from daalu_automation.core import kube_tools

    return {
        "tools": [
            {"name": n, "description": kube_tools.TOOLS[n].description,
             "input_schema": kube_tools.TOOLS[n].input_schema}
            for n in _read_tool_names()
        ]
    }


async def tool_run_read_tool(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from daalu_automation.core import kube_tools

    name = str(args.get("tool") or "")
    tool_input = args.get("input") or {}
    if not isinstance(tool_input, dict):
        raise ToolError("`input` must be a JSON object")
    if name not in kube_tools.TOOLS:
        raise ToolError(
            f"unknown tool {name!r}. Read tools: {', '.join(_read_tool_names())}"
        )
    if kube_tools.tool_requires_approval(name, tool_input) or name in _NOT_READ_ONLY:
        raise ToolError(
            f"{name} changes state and cannot be run over MCP. Use propose_action "
            "to attach it to an alert; the tenant's autonomy policy then decides "
            "whether a human must approve it."
        )
    output = await kube_tools.execute_tool(
        name, tool_input, tenant_id=ctx.tenant_id,
        **(await _cluster_target(ctx, args.get("cluster"))),
    )
    if output.startswith("error:"):
        raise ToolError(output)
    return {"tool": name, "output": output}


async def tool_recent_changes(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from daalu_automation.core import kube_tools

    hours = max(1, min(int(args.get("hours") or 24), 24 * 14))
    spec = kube_tools.TOOLS.get("recent_changes")
    if spec is not None:
        props = (spec.input_schema or {}).get("properties", {})
        wanted = {"namespace": args.get("namespace"), "hours": hours}
        tool_input = {k: v for k, v in wanted.items() if v is not None and (not props or k in props)}
        output = await kube_tools.execute_tool(
            "recent_changes", tool_input, tenant_id=ctx.tenant_id,
            **(await _cluster_target(ctx, args.get("cluster"))),
        )
    else:
        try:
            from daalu_automation.core.aiops import change_context
            fn = change_context.recent_changes
        except (ImportError, AttributeError) as e:
            raise ToolError("recent_changes is not available on this Daalu server") from e
        output = await fn(
            tenant_id=ctx.tenant_id, cluster=args.get("cluster"),
            namespace=args.get("namespace"), hours=hours,
        )
    if output.startswith("error:"):
        raise ToolError(output)
    return {"output": output, "hours": hours}


async def tool_propose_action(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from daalu_automation.api.routers import alert_chat
    from daalu_automation.core import kube_tools
    from daalu_automation.core.aiops import investigations as inv_mod
    from daalu_automation.core.aiops import policy as policy_mod
    from daalu_automation.models import ActionStatus, AlertAction, AlertChatMessage, ChatRole

    alert_id = _uuid_arg(args, "alert_id")
    name = str(args.get("tool") or "")
    tool_input = args.get("input") or {}
    if not isinstance(tool_input, dict):
        raise ToolError("`input` must be a JSON object")
    tool_input = dict(tool_input)
    if args.get("reason") and not tool_input.get(kube_tools.REASON_ARG):
        tool_input[kube_tools.REASON_ARG] = str(args["reason"])
    alert = await _load_alert(ctx, alert_id)
    if name not in kube_tools.TOOLS:
        raise ToolError(f"unknown tool {name!r}")
    if not kube_tools.tool_requires_approval(name, tool_input):
        raise ToolError(f"{name} is a read tool; run it with run_read_tool instead")
    tool_input, err = kube_tools.validate_write_input(name, tool_input)
    if err:
        raise ToolError(err)

    cluster = alert_chat._cluster_for_alert(alert)
    severity = alert.severity.value if alert.severity is not None else ""
    decision = await policy_mod.decide_for_tenant(
        ctx.db, ctx.tenant_id,
        policy_mod.write_request_for(
            name, tool_input, cluster=cluster, severity=severity, external=True,
        ),
    )
    if decision.mode == "deny":
        await audit.record(
            ctx.db, ctx.tenant_id, "action.denied", actor=ctx.actor, subject=str(alert.id),
            data={"tool": name, "input": tool_input, "decision": decision.as_dict(),
                  "external": True},
        )
        raise ToolError(
            f"denied by the autonomy policy ({decision.reason}). The action was not "
            "queued; propose a different remediation or hand it to an operator."
        )

    investigation = await inv_mod.ensure(ctx.db, tenant_id=ctx.tenant_id, alert_id=alert.id)
    tool_call_id = f"mcp_{uuid.uuid4().hex}"
    reason = str(tool_input.get(kube_tools.REASON_ARG) or "")
    shown_input = {k: v for k, v in tool_input.items() if k != kube_tools.REASON_ARG}
    msg = AlertChatMessage(
        tenant_id=ctx.tenant_id,
        alert_id=alert.id,
        role=ChatRole.assistant,
        content=(
            f"External agent {ctx.email} proposed "
            f"`{kube_tools.render_tool_call(name, shown_input)}` via MCP."
            + (f"\n\nReason: {reason}" if reason else "")
        ),
        tool_calls_json=[{"id": tool_call_id, "name": name, "input": tool_input}],
    )
    ctx.db.add(msg)
    await ctx.db.flush()

    action = AlertAction(
        tenant_id=ctx.tenant_id,
        alert_id=alert.id,
        message_id=msg.id,
        tool_call_id=tool_call_id,
        tool_name=name,
        tool_input=tool_input,
        requires_approval=True,
        status=ActionStatus.pending,
        policy_mode=decision.mode,
        policy_rule=decision.rule_id,
        policy_reason=decision.reason,
        approvals_required=decision.approvers,
        approvals=[],
        actor=ctx.actor,
    )
    action.preview = await alert_chat._preview_write(
        name, tool_input, tenant_id=ctx.tenant_id, cluster=cluster,
    )
    if decision.mode == "auto":
        action.status = ActionStatus.approved
        action.auto_executed = True
        action.approved_at = datetime.now(tz=timezone.utc)
        inv_mod.bump(investigation, "actions_auto")
    inv_mod.bump(investigation, "actions_proposed")
    ctx.db.add(action)
    await ctx.db.flush()
    await audit.record(
        ctx.db, ctx.tenant_id, "action.proposed", actor=ctx.actor, subject=str(alert.id),
        data={
            "action_id": str(action.id), "tool": name, "input": tool_input,
            "policy_mode": action.policy_mode, "policy_rule": action.policy_rule,
            "approvals_required": action.approvals_required, "external": True,
        },
    )
    result = {
        "action_id": action.id,
        "alert_id": alert.id,
        "tool": name,
        "input": tool_input,
        "status": action.status,
        "policy": decision.as_dict(),
        "approvals_required": action.approvals_required,
        "auto_executed": action.auto_executed,
        "preview": action.preview or {},
        "next": (
            "The policy allowed this action to run automatically; Daalu is executing "
            "and verifying it now. Check get_investigation for the outcome."
            if action.auto_executed else
            f"Waiting for {action.approvals_required} human approval(s) on the alert "
            "page in Daalu."
        ),
    }
    await ctx.db.commit()
    if action.auto_executed:
        await alert_chat._start_auto_actions(
            db=ctx.db, tenant_id=ctx.tenant_id, alert=alert, action_ids=[action.id],
        )
    return result


async def tool_get_policy(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from daalu_automation.api.routers import aiops_policy

    out = await aiops_policy._policy_out(
        ctx.db, ctx.tenant_id, with_schema=bool(args.get("include_schema")),
    )
    data = out.model_dump(mode="json")
    if data.get("json_schema") is None:
        data.pop("json_schema", None)
    return data


async def tool_simulate_policy(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from fastapi import HTTPException

    from daalu_automation.api.routers import aiops_policy

    if not args.get("tool"):
        raise ToolError("`tool` is required")
    try:
        body = aiops_policy.SimulateIn(
            tool=str(args["tool"]),
            input=args.get("input") or {},
            cluster=str(args.get("cluster") or ""),
            severity=str(args.get("severity") or ""),
            external=bool(args.get("external", True)),
            yaml=args.get("yaml"),
        )
    except Exception as e:  # pydantic ValidationError
        raise ToolError(f"invalid arguments: {e}") from e
    try:
        return await aiops_policy.simulate(body, db=ctx.db, tenant_id=ctx.tenant_id)
    except HTTPException as e:
        raise ToolError(_http_detail(e)) from e


def _lazy(module: str, attr: str) -> Callable[..., Any]:
    """Import a sibling AOMM function at call time; a missing one becomes a
    tool error instead of breaking the whole server."""
    import importlib

    try:
        fn = getattr(importlib.import_module(f"daalu_automation.core.aiops.{module}"), attr)
    except (ImportError, AttributeError) as e:
        raise ToolError(f"{module}.{attr} is not available on this Daalu server") from e
    return fn


def _runbook_out(row: Any) -> Any:
    try:
        to_out = _lazy("runbooks", "to_out")
    except ToolError:
        to_out = None
    d = _jsonable(to_out(row) if to_out and not isinstance(row, dict) else row)
    if isinstance(d, dict):
        d.pop("tenant_id", None)
    return d


async def tool_list_runbooks(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    fn = _lazy("runbooks", "list_runbooks")
    status = args.get("status") or None
    rows = await fn(ctx.db, ctx.tenant_id, status=status)
    items = []
    for r in rows or []:
        d = _runbook_out(r)
        if isinstance(d, dict):
            d.pop("body", None)  # listing: metadata only; get_runbook has the body
        items.append(d)
    return {"runbooks": items, "count": len(items)}


async def tool_get_runbook(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    fn = _lazy("runbooks", "get_runbook")
    key = str(args.get("id") or args.get("id_or_slug") or "")
    if not key:
        raise ToolError("`id` is required (runbook id or slug)")
    row = await fn(ctx.db, ctx.tenant_id, key)
    if row is None:
        raise ToolError(f"runbook {key!r} not found")
    return {"runbook": _runbook_out(row)}


async def tool_get_maturity(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    fn = _lazy("maturity", "compute")
    days = max(1, min(int(args.get("days") or 30), 365))
    return _jsonable(await fn(ctx.db, ctx.tenant_id, days=days))


async def tool_submit_verdict(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from fastapi import HTTPException

    from daalu_automation.api.routers import aiops_investigations as inv_router
    from daalu_automation.core.aiops import investigations as inv_mod

    verdict = args.get("verdict")
    if verdict not in inv_mod.VERDICTS:
        raise ToolError(f"`verdict` must be one of {list(inv_mod.VERDICTS)}")
    if args.get("investigation_id"):
        inv_id = str(_uuid_arg(args, "investigation_id"))
    else:
        alert_id = _uuid_arg(args, "alert_id")
        inv = await inv_mod.get_for_alert(ctx.db, alert_id)
        if inv is None or inv.tenant_id != ctx.tenant_id:
            raise ToolError(f"alert {alert_id} has no investigation to grade")
        inv_id = str(inv.id)
    body = inv_router.VerdictIn(verdict=verdict, note=str(args.get("note") or "")[:4000])
    try:
        out = await inv_router.submit_verdict(
            inv_id, body, db=ctx.db, tenant_id=ctx.tenant_id, user=ctx.user,
        )
    except HTTPException as e:
        raise ToolError(_http_detail(e)) from e
    return {"investigation": out}


_ALERT_ID = {"type": "string", "format": "uuid", "description": "Daalu alert id."}
_CLUSTER = {
    "type": "string",
    "description": "Cluster slug or kubernetes integration name (see list_clusters). "
                   "Omit for the tenant's default cluster.",
}


def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        s["required"] = required
    return s


def _build_tools() -> dict[str, Tool]:
    tools = [
        Tool(
            "list_alerts", "List alerts",
            "Recent alerts for this tenant, newest activity first, with their AI "
            "investigation status.",
            _schema({
                "status": {"type": "string",
                           "enum": ["open", "acknowledged", "resolved", "suppressed"]},
                "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
                "module": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            }),
            tool_list_alerts,
        ),
        Tool(
            "get_investigation", "Get an alert's investigation",
            "Daalu's AI investigation of one alert: root cause, actions proposed and "
            "their policy decisions and outcomes, resolution, verdict, and the latest "
            "write-up.",
            _schema({"alert_id": _ALERT_ID}, ["alert_id"]),
            tool_get_investigation,
        ),
        Tool(
            "list_clusters", "List clusters",
            "Kubernetes clusters this tenant has connected to Daalu.",
            _schema({}),
            tool_list_clusters,
        ),
        Tool(
            "list_read_tools", "List read-only tools",
            "The read-only kube / Prometheus / Loki / cloud tools run_read_tool can run, "
            "with their input schemas.",
            _schema({}),
            tool_list_read_tools,
        ),
        Tool(
            "run_read_tool", "Run a read-only tool",
            "Run one of Daalu's read-only investigation tools (pod logs, events, "
            "describe, deployments, rollout history, Prometheus, Loki, cloud reads). "
            "Write tools are refused: use propose_action. See list_read_tools for the "
            "names and their inputs.",
            _schema({
                "tool": {"type": "string", "enum": _read_tool_names()},
                "input": {"type": "object", "description": "The tool's arguments.",
                          "default": {}},
                "cluster": _CLUSTER,
            }, ["tool"]),
            tool_run_read_tool,
        ),
        Tool(
            "recent_changes", "Recent changes",
            "What changed recently in a namespace or cluster: Deployment rollouts and "
            "image changes, scaling events, Argo CD syncs. Usually the first thing to "
            "check when something broke.",
            _schema({
                "cluster": _CLUSTER,
                "namespace": {"type": "string"},
                "hours": {"type": "integer", "minimum": 1, "maximum": 336, "default": 24},
            }),
            tool_recent_changes,
        ),
        Tool(
            "propose_action", "Propose a remediation",
            "Attach a write (rollout_undo, scale_deployment, restart_deployment, "
            "delete_pod, patch_resource, non-GET call_external_api) to an alert. It is "
            "never run directly: the tenant's autonomy policy decides. Usually a human "
            "must approve it in Daalu; proposals from external agents only run "
            "automatically when the matching policy rule sets allow_external_auto. "
            "Denied writes return an error.",
            _schema({
                "alert_id": _ALERT_ID,
                "tool": {"type": "string",
                         "description": "Write tool name, e.g. rollout_undo."},
                "input": {"type": "object",
                          "description": "The write tool's arguments (namespace, name, ...)."},
                "reason": {"type": "string",
                           "description": "Why this fixes the alert and what will happen; "
                                          "shown to the approver. Required."},
            }, ["alert_id", "tool", "input", "reason"]),
            tool_propose_action, read_only=False, destructive=True,
        ),
        Tool(
            "get_policy", "Get the autonomy policy",
            "The tenant's autonomy policy (YAML and parsed), kill-switch and "
            "circuit-breaker state.",
            _schema({"include_schema": {"type": "boolean", "default": False}}),
            tool_get_policy,
        ),
        Tool(
            "simulate_policy", "Simulate a policy decision",
            "What the autonomy policy would decide (deny / approve / auto) for a write "
            "right now, optionally against a draft policy YAML. external defaults to "
            "true (an MCP proposal).",
            _schema({
                "tool": {"type": "string"},
                "input": {"type": "object", "default": {}},
                "cluster": {"type": "string"},
                "severity": {"type": "string", "enum": ["", "info", "warning", "critical"]},
                "external": {"type": "boolean", "default": True},
                "yaml": {"type": "string", "description": "Draft policy to test instead "
                                                          "of the saved one."},
            }, ["tool"]),
            tool_simulate_policy,
        ),
        Tool(
            "list_runbooks", "List runbooks",
            "Runbooks as code (metadata only).",
            _schema({"status": {"type": "string", "enum": ["draft", "published"]}}),
            tool_list_runbooks,
        ),
        Tool(
            "get_runbook", "Get a runbook",
            "One runbook, with its markdown body, by id or slug.",
            _schema({"id": {"type": "string", "description": "Runbook id or slug."}}, ["id"]),
            tool_get_runbook,
        ),
        Tool(
            "get_maturity", "Get the AI-Ops maturity level",
            "The tenant's AOMM level, the criteria behind it, metrics and next steps.",
            _schema({"days": {"type": "integer", "minimum": 1, "maximum": 365,
                              "default": 30}}),
            tool_get_maturity,
        ),
        Tool(
            "submit_verdict", "Grade an investigation",
            "Record whether Daalu's root cause for an alert was correct, partial or "
            "wrong. Give alert_id or investigation_id.",
            _schema({
                "alert_id": _ALERT_ID,
                "investigation_id": {"type": "string", "format": "uuid"},
                "verdict": {"type": "string", "enum": ["correct", "partial", "wrong"]},
                "note": {"type": "string", "maxLength": 4000},
            }, ["verdict"]),
            tool_submit_verdict, read_only=False,
        ),
    ]
    return {t.name: t for t in tools}


def tools() -> dict[str, Tool]:
    """The tool registry. Built per call: the read-tool enum follows
    ``kube_tools.TOOLS``, which other modules extend at import time."""
    return _build_tools()


# ── Dispatcher ──────────────────────────────────────────────────────────────


async def _call_tool(
    params: dict[str, Any], *, db: AsyncSession, tenant_id: uuid.UUID, user: Any, email: str,
) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    registry = tools()
    if not isinstance(name, str) or name not in registry:
        raise _RpcError(INVALID_PARAMS, f"unknown tool: {name!r}")
    if not isinstance(args, dict):
        raise _RpcError(INVALID_PARAMS, "`arguments` must be an object")
    ctx = CallContext(db=db, tenant_id=tenant_id, user=user, email=email)
    is_error = False
    error = ""
    try:
        result = _ok(await registry[name].handler(ctx, args))
    except ToolError as e:
        is_error, error = True, str(e)
        result = _err_result(error)
    except Exception as e:  # noqa: BLE001 — a tool bug must not kill the session
        logger.exception("aiops.mcp.tool_failed", tool=name)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        is_error, error = True, f"internal error: {type(e).__name__}: {e}"
        result = _err_result(error)
    await audit.record(
        db, tenant_id, "mcp.call", actor=email, subject=name,
        data={"tool": name, "arguments": _truncate(args), "is_error": is_error,
              **({"error": error[:500]} if is_error else {})},
    )
    await db.commit()
    return result


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def handle(
    message: Any, *, db: AsyncSession, tenant_id: uuid.UUID, user: Any,
) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns the response, or ``None`` when no
    response is due (notifications, and responses sent to us by the client)."""
    if not isinstance(message, dict):
        return rpc_error(None, INVALID_REQUEST, "expected a JSON-RPC 2.0 object "
                         "(batches are not supported in MCP 2025-06-18)")
    msg_id = message.get("id")
    is_notification = "id" not in message
    method = message.get("method")
    if message.get("jsonrpc") != "2.0":
        return None if is_notification else rpc_error(
            msg_id, INVALID_REQUEST, "jsonrpc must be \"2.0\"")
    if method is None:
        # A response to a server→client request. We never send any.
        if "result" in message or "error" in message:
            return None
        return rpc_error(msg_id, INVALID_REQUEST, "missing method")
    if not isinstance(method, str):
        return None if is_notification else rpc_error(
            msg_id, INVALID_REQUEST, "method must be a string")
    if is_notification:
        # notifications/initialized, notifications/cancelled, … : nothing to do.
        return None

    params = message.get("params") or {}
    if not isinstance(params, dict):
        return rpc_error(msg_id, INVALID_PARAMS, "params must be an object")
    email = str(getattr(user, "email", "") or "")
    try:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            return _rpc_result(msg_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": SERVER_TITLE,
                               "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _rpc_result(msg_id, {})
        if method == "tools/list":
            return _rpc_result(msg_id, {"tools": [t.definition() for t in tools().values()]})
        if method == "tools/call":
            return _rpc_result(msg_id, await _call_tool(
                params, db=db, tenant_id=tenant_id, user=user, email=email))
        return rpc_error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")
    except _RpcError as e:
        return rpc_error(msg_id, e.code, e.message)
    except Exception as e:  # noqa: BLE001
        logger.exception("aiops.mcp.handle_failed", method=method)
        return rpc_error(msg_id, INTERNAL_ERROR, f"internal error: {type(e).__name__}")
