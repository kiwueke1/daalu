"""Dashboard — per-namespace stats with HTMX auto-polling."""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from temporal_console import auth
from temporal_console.client import get_client
from temporal_console.config import get_settings

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["dashboard"])


@dataclass
class NamespaceStats:
    name: str
    display: str
    running: int
    completed_24h: int
    failed_24h: int
    error_rate_pct: float
    schedules: int


async def _namespace_stats(ns: str) -> NamespaceStats:
    settings = get_settings()
    client = await get_client(ns)
    # Temporal's count_workflows uses visibility; works on SQL visibility
    # stores without any custom search attributes beyond the built-ins.
    try:
        running = (await client.count_workflows(
            query="ExecutionStatus='Running'"
        )).count
    except Exception:
        running = 0
    try:
        completed = (await client.count_workflows(
            query="ExecutionStatus='Completed' AND CloseTime > '24h'"
        )).count
    except Exception:
        completed = 0
    try:
        failed = (await client.count_workflows(
            query="ExecutionStatus='Failed' AND CloseTime > '24h'"
        )).count
    except Exception:
        failed = 0

    total_closed = completed + failed
    rate = round(100.0 * failed / total_closed, 1) if total_closed else 0.0

    # Schedule count — list with a small page size to avoid loading all.
    schedule_count = 0
    try:
        async for _sch in client.list_schedules(page_size=100):
            schedule_count += 1
    except Exception:
        pass

    return NamespaceStats(
        name=ns,
        display=settings.namespace_display(ns),
        running=running,
        completed_24h=completed,
        failed_24h=failed,
        error_rate_pct=rate,
        schedules=schedule_count,
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _user: dict = Depends(auth.require_auth),
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "dashboard.html.j2", {"current_path": "/"},
    )


@router.get("/partials/dashboard-stats", response_class=HTMLResponse)
async def dashboard_stats_partial(
    request: Request,
    _user: dict = Depends(auth.require_auth),
) -> HTMLResponse:
    """HTMX polling target. Returns just the cards, not the full layout."""
    settings = get_settings()
    stats = []
    for ns in settings.namespaces:
        try:
            stats.append(await _namespace_stats(ns))
        except Exception as exc:
            logger.warning("dashboard.stats_failed", namespace=ns, error=str(exc))
    return request.app.state.templates.TemplateResponse(
        request,
        "_partials/dashboard_stats.html.j2",
        {"stats": stats},
    )
