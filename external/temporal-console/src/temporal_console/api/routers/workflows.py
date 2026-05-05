"""Workflows — list, detail, history, start-new form."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
UTC = timezone.utc
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from temporalio.common import WorkflowIDReusePolicy

from temporal_console import auth
from temporal_console.client import get_client
from temporal_console.config import get_settings

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["workflows"])


# Map Temporal status enum → (label, css class). Keeps the template dumb.
STATUS_STYLE = {
    "Running":           ("Running",     "running"),
    "Completed":         ("Completed",   "success"),
    "Failed":            ("Failed",      "danger"),
    "Canceled":          ("Canceled",    "warning"),
    "Terminated":        ("Terminated",  "danger"),
    "ContinuedAsNew":    ("Continued",   "neutral"),
    "TimedOut":          ("Timed out",   "danger"),
}


@dataclass
class WorkflowRow:
    id: str
    run_id: str
    type: str
    status: str
    status_label: str
    status_class: str
    namespace: str
    start: datetime | None
    end: datetime | None
    task_queue: str


def _row_from_execution(exc, namespace: str) -> WorkflowRow:
    status_name = exc.status.name if exc.status else "Running"
    label, css = STATUS_STYLE.get(status_name, (status_name, "neutral"))
    return WorkflowRow(
        id=exc.id,
        run_id=exc.run_id,
        type=exc.workflow_type,
        status=status_name,
        status_label=label,
        status_class=css,
        namespace=namespace,
        start=exc.start_time,
        end=exc.close_time,
        task_queue=exc.task_queue or "",
    )


@router.get("/workflows", response_class=HTMLResponse)
async def list_workflows(
    request: Request,
    _user: dict = Depends(auth.require_auth),
    namespace: str | None = None,
    status: str | None = None,
    type_contains: str | None = None,
    page_size: int = 50,
) -> HTMLResponse:
    settings = get_settings()
    ns = namespace or settings.namespaces[0]
    if ns not in settings.namespaces:
        raise HTTPException(400, f"unknown namespace {ns}")

    # Build a Temporal visibility query from the filters.
    parts: list[str] = []
    if status:
        parts.append(f"ExecutionStatus='{status}'")
    if type_contains:
        # Temporal list queries don't support LIKE on WorkflowType; use
        # an exact match if the user typed a full name, otherwise skip.
        parts.append(f"WorkflowType='{type_contains}'")
    query = " AND ".join(parts) if parts else ""

    rows: list[WorkflowRow] = []
    client = await get_client(ns)
    try:
        async for exc in client.list_workflows(query=query, page_size=page_size):
            rows.append(_row_from_execution(exc, ns))
            if len(rows) >= page_size:
                break
    except Exception as exc_err:
        logger.warning("workflows.list_failed", namespace=ns, error=str(exc_err))

    return request.app.state.templates.TemplateResponse(
        request,
        "workflows.html.j2",
        {
            "current_path": "/workflows",
            "rows": rows,
            "namespace": ns,
            "filter_status": status or "",
            "filter_type": type_contains or "",
            "statuses": list(STATUS_STYLE.keys()),
        },
    )


@router.get("/workflows/{workflow_id}/progress.html", response_class=HTMLResponse)
async def workflow_progress_partial(
    request: Request,
    workflow_id: str,
    namespace: str | None = None,
    _user: dict = Depends(auth.require_auth),
) -> HTMLResponse:
    """
    Return an HTML fragment with collapsible per-stage progress cards.

    Polled every 2s by HTMX from the workflow detail page. Reads:
      1. The workflow's own ``progress`` query for the structured stage list.
      2. ``describe()`` to find the currently-running activity, then pulls
         its last heartbeat detail (a rolling tail of stdout) so the
         "running" stage shows live output even though the activity has not
         yet completed.
    """
    settings = get_settings()
    ns = namespace or settings.namespaces[0]
    if ns not in settings.namespaces:
        raise HTTPException(400, f"unknown namespace {ns}")

    client = await get_client(ns)
    handle = client.get_workflow_handle(workflow_id)

    # 1. Pull workflow-side progress (typed dict — workers serialise dataclasses
    #    as plain dicts).
    progress: dict[str, Any] | None = None
    try:
        result = await handle.query("progress")
        progress = result if isinstance(result, dict) else _to_dict(result)
    except Exception as exc:
        logger.info(
            "workflow.progress_query_unavailable",
            workflow_id=workflow_id, error=str(exc),
        )

    # 2. Live tail from the currently-running activity, if any.
    live_tail: list[str] = []
    live_stage: str | None = None
    try:
        desc = await handle.describe()
        pas = list(getattr(desc, "pending_activities", []) or [])
        if pas:
            pa = pas[0]
            # heartbeat_details: list[Any]; activity sends {"tail": [...], "n": N}
            hb = list(getattr(pa, "heartbeat_details", []) or [])
            for d in reversed(hb):
                if isinstance(d, dict) and d.get("tail"):
                    live_tail = list(d["tail"])
                    break
            live_stage = getattr(pa, "activity_type", None)
    except Exception as exc:
        logger.info(
            "workflow.live_tail_unavailable",
            workflow_id=workflow_id, error=str(exc),
        )

    return request.app.state.templates.TemplateResponse(
        request,
        "_partials/workflow_progress.html.j2",
        {
            "workflow_id": workflow_id,
            "namespace": ns,
            "progress": progress,
            "live_tail": live_tail,
            "live_stage": live_stage,
        },
    )


def _to_dict(obj: Any) -> dict[str, Any]:
    """Best-effort convert dataclass-like SDK return into a plain dict."""
    if hasattr(obj, "__dict__"):
        return {k: _to_dict(v) for k, v in obj.__dict__.items()}
    if isinstance(obj, list):
        return [_to_dict(v) for v in obj]  # type: ignore[return-value]
    return obj


@router.get("/workflows/{workflow_id}", response_class=HTMLResponse)
async def workflow_detail(
    request: Request,
    workflow_id: str,
    namespace: str | None = None,
    _user: dict = Depends(auth.require_auth),
) -> HTMLResponse:
    settings = get_settings()
    ns = namespace or settings.namespaces[0]
    client = await get_client(ns)
    try:
        handle = client.get_workflow_handle(workflow_id)
        desc = await handle.describe()
    except Exception as exc:
        raise HTTPException(404, f"workflow not found: {exc}")

    # Gather the history as an event list for the timeline view. Temporal
    # events can be numerous; cap at 200 most-recent.
    events: list[dict[str, Any]] = []
    try:
        async for ev in handle.fetch_history_events():
            events.append({
                "id": ev.event_id,
                "type": ev.event_type.name if ev.event_type else "Unknown",
                "time": datetime.fromtimestamp(ev.event_time.seconds, tz=UTC) if ev.event_time else None,
                "task_id": ev.task_id,
            })
            if len(events) >= 200:
                break
    except Exception as exc:
        logger.warning("workflow.history_failed", workflow_id=workflow_id, error=str(exc))

    status_name = desc.status.name if desc.status else "Running"
    label, css = STATUS_STYLE.get(status_name, (status_name, "neutral"))

    return request.app.state.templates.TemplateResponse(
        request,
        "workflow_detail.html.j2",
        {
            "current_path": "/workflows",
            "namespace": ns,
            "workflow_id": workflow_id,
            "run_id": desc.run_id,
            "type": desc.workflow_type,
            "status_label": label,
            "status_class": css,
            "status_name": status_name,
            "task_queue": desc.task_queue,
            "start": desc.start_time,
            "end": desc.close_time,
            "events": list(reversed(events)),  # newest first
            "deep_link": settings.deep_link_for(workflow_id),
        },
    )


@router.get("/workflows/new", response_class=HTMLResponse)
async def new_workflow_form(
    request: Request,
    _user: dict = Depends(auth.require_auth),
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "workflow_new.html.j2",
        {"current_path": "/workflows"},
    )


@router.post("/workflows/new", response_class=HTMLResponse)
async def start_workflow(
    request: Request,
    _user: dict = Depends(auth.require_auth),
    namespace: str = Form(...),
    workflow_type: str = Form(...),
    workflow_id: str = Form(""),
    task_queue: str = Form(...),
    payload_json: str = Form(""),
) -> HTMLResponse:
    """Start an arbitrary workflow.

    `payload_json` is parsed as JSON — its top-level value becomes the
    single positional arg to the workflow's `run` method, matching
    Temporal Python SDK convention (most workflows take one dataclass).
    Use `null` to pass no args.
    """
    settings = get_settings()
    if namespace not in settings.namespaces:
        raise HTTPException(400, f"unknown namespace {namespace}")

    try:
        payload = json.loads(payload_json) if payload_json.strip() else None
    except json.JSONDecodeError as exc:
        return request.app.state.templates.TemplateResponse(
            request,
            "workflow_new.html.j2",
            {
                "current_path": "/workflows",
                "error": f"invalid JSON: {exc}",
                "workflow_type": workflow_type,
                "task_queue": task_queue,
                "payload_json": payload_json,
                "namespace": namespace,
                "workflow_id_in": workflow_id,
            },
            status_code=400,
        )

    client = await get_client(namespace)
    import uuid as _uuid
    wf_id = workflow_id.strip() or f"adhoc-{_uuid.uuid4()}"
    args = [payload] if payload is not None else []
    try:
        await client.start_workflow(
            workflow_type,
            *args,
            id=wf_id,
            task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    except Exception as exc:
        return request.app.state.templates.TemplateResponse(
            request,
            "workflow_new.html.j2",
            {
                "current_path": "/workflows",
                "error": f"start failed: {exc}",
                "workflow_type": workflow_type,
                "task_queue": task_queue,
                "payload_json": payload_json,
                "namespace": namespace,
                "workflow_id_in": workflow_id,
            },
            status_code=500,
        )

    logger.info("workflow.started", namespace=namespace, workflow_id=wf_id, type=workflow_type)
    return RedirectResponse(
        url=f"/workflows/{wf_id}?namespace={namespace}",
        status_code=303,
    )
