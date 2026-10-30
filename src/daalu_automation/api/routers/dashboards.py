"""Dashboards API — Grafana-backed, rendered natively in Daalu.

Three things the UI needs:

* **Browse** the tenant's Grafana dashboards (``GET /dashboards``) and open one
  (``GET /dashboards/{uid}``) as a flat panel spec.
* **Fill** each panel by running its PromQL/LogQL against the tenant's own
  Prometheus/Loki (``POST /dashboards/query``) — Grafana is never in the data
  path, so charts render with the same native components as the AI-factory GPU
  panels.
* **Create** a dashboard from a natural-language prompt (``POST
  /dashboards/ai``): an LLM emits a Grafana dashboard model, we POST it to
  Grafana, and it joins the same list as every hand-built board.

See :mod:`daalu_automation.core.grafana` and
:mod:`daalu_automation.core.tenant_metrics`.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from daalu_automation.api.deps import current_admin, current_tenant_id
from daalu_automation.core import grafana, tenant_metrics
from daalu_automation.core.grafana import GrafanaUnavailable
from daalu_automation.core.llm import LLMUnavailable, complete_json
from daalu_automation.core.tenant_metrics import MetricsUnavailable
from daalu_automation.models import User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/dashboards", tags=["dashboards"])

# Range token → window seconds. Mirrors the AI-factory switcher so the two
# native-chart surfaces feel identical.
_RANGES = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}


# ── schemas ──────────────────────────────────────────────────────────────


class DashboardSummaryOut(BaseModel):
    uid: str
    title: str
    folder: str | None
    tags: list[str]
    source: str


class DashboardListOut(BaseModel):
    configured: bool
    dashboards: list[DashboardSummaryOut]
    error: str | None = None


class PanelOut(BaseModel):
    id: str
    title: str
    type: str
    datasource: str
    expr: str
    unit: str | None
    w: int
    h: int
    x: int
    y: int


class DashboardOut(BaseModel):
    uid: str
    title: str
    description: str
    tags: list[str]
    source: str
    panels: list[PanelOut]


class QueryIn(BaseModel):
    datasource: Literal["prometheus", "loki"]
    expr: str = Field(..., min_length=1, max_length=4000)
    kind: Literal["timeseries", "stat", "logs", "table"] = "timeseries"
    range: str = "6h"


class SeriesPointOut(BaseModel):
    ts: int
    value: float


class SeriesOut(BaseModel):
    name: str
    points: list[SeriesPointOut]


class LogLineOut(BaseModel):
    ts: int
    line: str
    labels: dict[str, str]


class QueryOut(BaseModel):
    kind: str
    # timeseries → series; stat/table → instant; logs → lines
    series: list[SeriesOut] = []
    instant: list[dict[str, Any]] = []
    logs: list[LogLineOut] = []
    unit: str | None = None
    error: str | None = None


class AiCreateIn(BaseModel):
    prompt: str = Field(..., min_length=4, max_length=2000)
    # Hint to bias panel datasources; the model may still mix where sensible.
    datasource: Literal["prometheus", "loki", "mixed"] = "prometheus"
    title: str | None = Field(default=None, max_length=120)


class AiCreateOut(BaseModel):
    uid: str
    title: str
    url: str | None
    panel_count: int


# ── browse ───────────────────────────────────────────────────────────────


@router.get("", response_model=DashboardListOut)
async def list_dashboards(tenant_id: uuid.UUID = Depends(current_tenant_id)):
    try:
        rows = await grafana.list_dashboards(tenant_id)
    except GrafanaUnavailable as e:
        # Not configured / unreachable is an expected, recoverable state — the
        # UI shows a "connect Grafana" affordance rather than an error toast.
        return DashboardListOut(configured=False, dashboards=[], error=str(e))
    return DashboardListOut(
        configured=True,
        dashboards=[
            DashboardSummaryOut(
                uid=r.uid, title=r.title, folder=r.folder, tags=r.tags,
                source=r.source,
            )
            for r in rows
        ],
    )


@router.get("/{uid}", response_model=DashboardOut)
async def get_dashboard(
    uid: str, tenant_id: uuid.UUID = Depends(current_tenant_id)
):
    try:
        spec = await grafana.get_dashboard(tenant_id, uid)
    except GrafanaUnavailable as e:
        raise HTTPException(502, str(e)) from e
    return DashboardOut(
        uid=spec.uid,
        title=spec.title,
        description=spec.description,
        tags=spec.tags,
        source=spec.source,
        panels=[
            PanelOut(
                id=p.id, title=p.title, type=p.type, datasource=p.datasource,
                expr=p.expr, unit=p.unit, w=p.w, h=p.h, x=p.x, y=p.y,
            )
            for p in spec.panels
        ],
    )


# ── data plane (native render) ───────────────────────────────────────────


@router.post("/query", response_model=QueryOut)
async def query_panel(
    payload: QueryIn, tenant_id: uuid.UUID = Depends(current_tenant_id)
):
    """Run one panel's query against the tenant's metric/log store."""
    window = _RANGES.get(payload.range, 21600)
    end = time.time()
    start = end - window
    try:
        if payload.datasource == "loki" or payload.kind == "logs":
            lines = await tenant_metrics.query_logs(
                tenant_id, payload.expr, start=start, end=end, limit=200
            )
            return QueryOut(
                kind="logs",
                logs=[
                    LogLineOut(ts=line.ts, line=line.line, labels=line.labels)
                    for line in lines
                ],
            )
        if payload.kind in ("stat", "table"):
            vals = await tenant_metrics.query_instant(tenant_id, payload.expr)
            return QueryOut(
                kind=payload.kind,
                instant=[
                    {"name": v.name, "value": v.value, "labels": v.labels}
                    for v in vals
                ],
            )
        # timeseries
        step = max(15, window // 120)
        series = await tenant_metrics.query_range(
            tenant_id, payload.expr, start=start, end=end, step_s=step
        )
        return QueryOut(
            kind="timeseries",
            series=[
                SeriesOut(
                    name=s.name,
                    points=[SeriesPointOut(ts=p.ts, value=p.value) for p in s.points],
                )
                for s in series
            ],
        )
    except MetricsUnavailable as e:
        # Surface as a soft error in the panel, not a 5xx — one broken panel
        # shouldn't blank the whole dashboard.
        return QueryOut(kind=payload.kind, error=str(e))


# ── AI create ────────────────────────────────────────────────────────────

_AI_SYSTEM = """You are a Grafana dashboard generator for a Kubernetes \
observability platform. Given a request, output a SINGLE JSON object that is a \
valid Grafana dashboard model (schemaVersion 39).

Hard requirements:
- Top-level keys: "title" (string), "tags" (array of strings, include \
"daalu" and "ai-generated"), "schemaVersion": 39, "timezone": "browser", \
"panels" (array).
- Each panel object MUST have: "type" (one of "timeseries", "stat", "gauge", \
"table", "logs"), "title", "gridPos" ({"h","w","x","y"} on a 24-column grid), \
and "targets" (array). Lay panels out left-to-right, top-to-bottom, no overlaps.
- Each target MUST have "expr" set to a valid PromQL query (for metric \
panels) or a LogQL query (for "logs" panels), plus "refId" ("A","B",...).
- For metric panels set "datasource": {"type":"prometheus"}. For logs panels \
set "datasource": {"type":"loki"}.
- Set a sensible unit in panel "fieldConfig":{"defaults":{"unit": "..."}} \
(e.g. "percent", "bytes", "short", "s", "reqps").
- Prefer standard kube-state-metrics / cAdvisor / node-exporter metric names \
(e.g. container_cpu_usage_seconds_total, container_memory_working_set_bytes, \
kube_pod_status_phase, kube_deployment_status_replicas_unavailable, \
node_cpu_seconds_total, up).
- Use rate()/sum by()/avg by() appropriately. 4-8 panels is ideal.

Output ONLY the JSON object. No markdown, no prose."""


@router.post("/ai", response_model=AiCreateOut)
async def ai_create_dashboard(
    payload: AiCreateIn,
    user: User = Depends(current_admin),
):
    """Generate a Grafana dashboard from a natural-language prompt and save it."""
    ds_hint = {
        "prometheus": "Use Prometheus/PromQL panels only.",
        "loki": "Favour Loki/LogQL log panels where it makes sense.",
        "mixed": "Mix metric and log panels as appropriate.",
    }[payload.datasource]
    user_msg = payload.prompt.strip()
    if payload.title:
        user_msg += f"\n\nUse this dashboard title: {payload.title!r}."
    user_msg += f"\n\n{ds_hint}"

    try:
        model = await complete_json(
            system=_AI_SYSTEM,
            user=user_msg,
            tenant_id=user.tenant_id,
            tier="quality",
            purpose="default",
            max_tokens=4096,
            source="dashboards.ai_create",
        )
    except (LLMUnavailable, ValueError) as e:
        raise HTTPException(502, f"dashboard generation failed — {e}") from e

    panels = model.get("panels")
    if not isinstance(panels, list) or not panels:
        raise HTTPException(
            502,
            "the model did not return any panels — try a more specific prompt",
        )
    # Defensive: ensure the required scaffolding the renderer + Grafana expect.
    model.setdefault("schemaVersion", 39)
    model.setdefault("timezone", "browser")
    tags = set(model.get("tags") or [])
    tags.update({"daalu", "ai-generated"})
    model["tags"] = sorted(tags)
    if payload.title:
        model["title"] = payload.title
    model.setdefault("title", "AI dashboard")

    try:
        resp = await grafana.create_dashboard(user.tenant_id, model)
    except GrafanaUnavailable as e:
        raise HTTPException(502, str(e)) from e

    logger.info(
        "dashboards.ai_created",
        tenant_id=str(user.tenant_id),
        uid=resp.get("uid"),
        panels=len(panels),
    )
    return AiCreateOut(
        uid=resp.get("uid") or "",
        title=model.get("title", "AI dashboard"),
        url=resp.get("url"),
        panel_count=len(panels),
    )
