"""Schedules — list, detail, pause/resume/trigger."""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from temporal_console import auth
from temporal_console.client import get_client
from temporal_console.config import get_settings

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["schedules"])


@dataclass
class ScheduleRow:
    id: str
    state: str
    cron_exprs: list[str]
    paused: bool
    namespace: str
    note: str


@router.get("/schedules", response_class=HTMLResponse)
async def list_schedules(
    request: Request,
    namespace: str | None = None,
    _user: dict = Depends(auth.require_auth),
) -> HTMLResponse:
    settings = get_settings()
    ns = namespace or settings.namespaces[0]
    if ns not in settings.namespaces:
        raise HTTPException(400, f"unknown namespace {ns}")

    rows: list[ScheduleRow] = []
    client = await get_client(ns)
    try:
        async for sch in client.list_schedules(page_size=100):
            # SDK types vary; best-effort introspection so the UI doesn't
            # blow up on a shape change.
            try:
                info = sch.info
                cron_exprs = []
                for spec in (getattr(sch.schedule, "spec", None) or None,):
                    if spec and getattr(spec, "cron_expressions", None):
                        cron_exprs = list(spec.cron_expressions)
                rows.append(ScheduleRow(
                    id=sch.id,
                    state=(info.state.note if info.state else "") or "",
                    cron_exprs=cron_exprs,
                    paused=bool(info.state.paused) if info.state else False,
                    namespace=ns,
                    note=(info.state.note if info.state else "") or "",
                ))
            except Exception as exc:
                logger.warning("schedule.parse_failed", schedule_id=getattr(sch, "id", "?"), error=str(exc))
    except Exception as exc:
        logger.warning("schedules.list_failed", namespace=ns, error=str(exc))

    return request.app.state.templates.TemplateResponse(
        request,
        "schedules.html.j2",
        {"current_path": "/schedules", "rows": rows, "namespace": ns},
    )


@router.post("/schedules/{schedule_id}/pause", response_class=HTMLResponse)
async def pause_schedule(
    request: Request, schedule_id: str,
    namespace: str = Form(...),
    _user: dict = Depends(auth.require_auth),
):
    client = await get_client(namespace)
    handle = client.get_schedule_handle(schedule_id)
    try:
        await handle.pause()
    except Exception as exc:
        raise HTTPException(500, f"pause failed: {exc}")
    return RedirectResponse(url=f"/schedules?namespace={namespace}", status_code=303)


@router.post("/schedules/{schedule_id}/unpause", response_class=HTMLResponse)
async def unpause_schedule(
    request: Request, schedule_id: str,
    namespace: str = Form(...),
    _user: dict = Depends(auth.require_auth),
):
    client = await get_client(namespace)
    handle = client.get_schedule_handle(schedule_id)
    try:
        await handle.unpause()
    except Exception as exc:
        raise HTTPException(500, f"unpause failed: {exc}")
    return RedirectResponse(url=f"/schedules?namespace={namespace}", status_code=303)


@router.post("/schedules/{schedule_id}/trigger", response_class=HTMLResponse)
async def trigger_schedule(
    request: Request, schedule_id: str,
    namespace: str = Form(...),
    _user: dict = Depends(auth.require_auth),
):
    client = await get_client(namespace)
    handle = client.get_schedule_handle(schedule_id)
    try:
        await handle.trigger()
    except Exception as exc:
        raise HTTPException(500, f"trigger failed: {exc}")
    return RedirectResponse(url=f"/schedules?namespace={namespace}", status_code=303)
