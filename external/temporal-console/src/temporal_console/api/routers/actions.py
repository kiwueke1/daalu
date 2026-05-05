"""Workflow-level actions — signal, terminate, cancel."""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from temporal_console import auth
from temporal_console.client import get_client

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["actions"])


def _back(namespace: str, workflow_id: str) -> str:
    return f"/workflows/{workflow_id}?namespace={namespace}"


@router.post("/workflows/{workflow_id}/terminate")
async def terminate(
    request: Request, workflow_id: str,
    namespace: str = Form(...),
    reason: str = Form(""),
    _user: dict = Depends(auth.require_auth),
):
    client = await get_client(namespace)
    handle = client.get_workflow_handle(workflow_id)
    try:
        await handle.terminate(reason or "terminated via console")
    except Exception as exc:
        raise HTTPException(500, f"terminate failed: {exc}")
    logger.info("workflow.terminated", workflow_id=workflow_id, namespace=namespace)
    return RedirectResponse(url=_back(namespace, workflow_id), status_code=303)


@router.post("/workflows/{workflow_id}/cancel")
async def cancel(
    request: Request, workflow_id: str,
    namespace: str = Form(...),
    _user: dict = Depends(auth.require_auth),
):
    client = await get_client(namespace)
    handle = client.get_workflow_handle(workflow_id)
    try:
        await handle.cancel()
    except Exception as exc:
        raise HTTPException(500, f"cancel failed: {exc}")
    return RedirectResponse(url=_back(namespace, workflow_id), status_code=303)


@router.post("/workflows/{workflow_id}/signal")
async def signal(
    request: Request, workflow_id: str,
    namespace: str = Form(...),
    signal_name: str = Form(...),
    payload_json: str = Form(""),
    _user: dict = Depends(auth.require_auth),
):
    payload = None
    if payload_json.strip():
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"bad JSON payload: {exc}")
    client = await get_client(namespace)
    handle = client.get_workflow_handle(workflow_id)
    try:
        if payload is None:
            await handle.signal(signal_name)
        else:
            await handle.signal(signal_name, payload)
    except Exception as exc:
        raise HTTPException(500, f"signal failed: {exc}")
    logger.info("workflow.signaled",
                workflow_id=workflow_id, signal=signal_name, namespace=namespace)
    return RedirectResponse(url=_back(namespace, workflow_id), status_code=303)
