"""Typed start-form router — drives a UI from the workflow registry."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict
from typing import Any, Dict, List

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from temporalio.common import WorkflowIDReusePolicy

from temporal_console import auth
from temporal_console import registry as wf_registry
from temporal_console.client import get_client
from temporal_console.config import get_settings

log = structlog.get_logger(__name__)
router = APIRouter(tags=["workflow-start"])


# ---------------------------------------------------------------------------
# JSON registry endpoint
# ---------------------------------------------------------------------------

@router.get("/api/registry")
async def get_registry(
    _user: dict = Depends(auth.require_auth),
) -> JSONResponse:
    """Return the full registry as JSON. Useful for debugging / external UIs."""
    return JSONResponse(
        {
            "workflows": [
                {**asdict(s), "fields": [asdict(f) for f in s.fields]}
                for s in wf_registry.all_workflows()
            ]
        }
    )


# ---------------------------------------------------------------------------
# Pick-a-workflow page
# ---------------------------------------------------------------------------

@router.get("/workflows/start", response_class=HTMLResponse)
async def list_startable(
    request: Request,
    _user: dict = Depends(auth.require_auth),
) -> HTMLResponse:
    """Show all registered workflow types as a tile grid the user can click."""
    return request.app.state.templates.TemplateResponse(
        request,
        "workflow_start_index.html.j2",
        {
            "current_path": "/workflows",
            "workflows": wf_registry.all_workflows(),
        },
    )


# ---------------------------------------------------------------------------
# Per-workflow typed form
# ---------------------------------------------------------------------------

@router.get("/workflows/start/{workflow_type}", response_class=HTMLResponse)
async def show_start_form(
    request: Request,
    workflow_type: str,
    _user: dict = Depends(auth.require_auth),
) -> HTMLResponse:
    schema = wf_registry.get(workflow_type)
    if schema is None:
        raise HTTPException(
            404,
            f"workflow type '{workflow_type}' is not in the registry. "
            f"Use /workflows/new for raw JSON input.",
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "workflow_start_form.html.j2",
        {
            "current_path": "/workflows",
            "schema": schema,
            "values": {},
            "error": None,
        },
    )


@router.post("/workflows/start/{workflow_type}", response_class=HTMLResponse)
async def submit_start_form(
    request: Request,
    workflow_type: str,
    _user: dict = Depends(auth.require_auth),
) -> HTMLResponse:
    """
    Translate form fields into a single dataclass-shaped dict, start the
    workflow on the registered task queue, and redirect to the detail page
    (which now has the per-stage progress view).
    """
    schema = wf_registry.get(workflow_type)
    if schema is None:
        raise HTTPException(404, f"unknown workflow type: {workflow_type}")

    settings = get_settings()
    form = await request.form()
    namespace = form.get("namespace") or settings.namespaces[0]
    if namespace not in settings.namespaces:
        raise HTTPException(400, f"unknown namespace: {namespace}")

    payload, errors = _build_payload(schema, form)
    if errors:
        return request.app.state.templates.TemplateResponse(
            request,
            "workflow_start_form.html.j2",
            {
                "current_path": "/workflows",
                "schema": schema,
                "values": _form_to_values(form),
                "error": "; ".join(errors),
                "namespace": namespace,
            },
            status_code=400,
        )

    # Workflow ID — explicit user value wins, else a UUID-suffixed default.
    wf_id = (form.get("workflow_id") or "").strip()
    if not wf_id:
        ts = time.strftime("%Y%m%d-%H%M%S")
        wf_id = f"{schema.workflow_type.lower()}-{ts}-{uuid.uuid4().hex[:6]}"

    client = await get_client(namespace)
    try:
        await client.start_workflow(
            schema.workflow_type,
            payload,
            id=wf_id,
            task_queue=schema.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("workflow.start_failed", workflow_type=workflow_type, error=str(exc))
        return request.app.state.templates.TemplateResponse(
            request,
            "workflow_start_form.html.j2",
            {
                "current_path": "/workflows",
                "schema": schema,
                "values": _form_to_values(form),
                "error": f"start failed: {exc}",
                "namespace": namespace,
            },
            status_code=500,
        )

    log.info(
        "workflow.started",
        workflow_type=workflow_type,
        workflow_id=wf_id,
        task_queue=schema.task_queue,
        namespace=namespace,
    )
    return RedirectResponse(
        url=f"/workflows/{wf_id}?namespace={namespace}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Form → payload coercion
# ---------------------------------------------------------------------------

def _build_payload(
    schema: wf_registry.WorkflowSchema, form: Any
) -> tuple[Dict[str, Any], List[str]]:
    """
    Translate raw form fields into the dataclass-shaped dict the workflow
    expects. Returns (payload, errors).

    Coercion rules:
      - bool checkboxes: present (any value) → True, absent → False
      - int: parsed via int(); empty → field default (or 0)
      - secret/string: passed through; empty optional fields → field default
        if non-None, else dropped from the payload (so the @dataclass default
        applies on the worker side)
    """
    payload: Dict[str, Any] = {}
    errors: List[str] = []

    for f in schema.fields:
        raw = form.get(f.name)
        if f.type == "bool":
            payload[f.name] = bool(raw)
            continue

        # Strings (incl. secret/textarea/choices)
        s = str(raw).strip() if raw is not None else ""
        if not s:
            if f.required:
                errors.append(f"'{f.label}' is required")
            elif f.default is not None:
                payload[f.name] = f.default
            # else: leave unset — server side dataclass default kicks in
            continue

        if f.type == "int":
            try:
                payload[f.name] = int(s)
            except ValueError:
                errors.append(f"'{f.label}' must be an integer")
            continue

        payload[f.name] = s

    return payload, errors


def _form_to_values(form: Any) -> Dict[str, Any]:
    """Form → dict for re-rendering the form on validation error."""
    return {k: form.get(k) for k in form.keys()}
