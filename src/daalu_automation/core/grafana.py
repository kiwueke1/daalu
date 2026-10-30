"""Grafana as Daalu's dashboard backend.

Daalu does **not** iframe Grafana. Instead it treats Grafana as a *catalog*
of dashboard definitions: we read a dashboard's panel list + each panel's
PromQL/LogQL via Grafana's HTTP API, then render the panels **natively** in
the Daalu UI by querying the tenant's Prometheus/Loki ourselves (see
:mod:`daalu_automation.core.tenant_metrics`). This mirrors the AI-factory GPU
dashboards — "the same observability ops sees in Grafana, but native and
role-scoped, no Grafana detour".

Two consequences:

* The dashboard *store* is Grafana. AI-created dashboards are POSTed back to
  Grafana (`/api/dashboards/db`), so they show up in the same list as every
  hand-built dashboard — no parallel Daalu-side dashboard table to keep in
  sync.
* The data plane is the tenant's own metric stack, reached through the same
  per-tenant Integration rows (+ cluster tunnel proxy) the alert-chat tools
  use. Grafana never proxies a single query datapoint.

Connection: a ``grafana`` Integration row carries ``{url, api_token}`` (a
Grafana service-account / API token). When the row also sets
``cluster_tunnel_id`` the calls are dialed through the daalu-edge forward
proxy, exactly like the other observability integrations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from sqlalchemy import select

from daalu_automation.core.cluster_proxy import get_proxy_url
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import Integration

logger = structlog.get_logger(__name__)


class GrafanaUnavailable(RuntimeError):
    """No grafana integration configured, or Grafana is unreachable."""


_HTTP_TIMEOUT_S = 20


# ── Daalu-side dashboard spec (the shape the UI renders) ─────────────────


# Grafana panel ``type`` → the renderer Daalu uses. Anything unmapped falls
# back to a timeseries (the safe, most-common shape).
_PANEL_TYPE_MAP = {
    "timeseries": "timeseries",
    "graph": "timeseries",
    "stat": "stat",
    "gauge": "stat",
    "singlestat": "stat",
    "bargauge": "stat",
    "table": "table",
    "logs": "logs",
    "piechart": "timeseries",
    "heatmap": "timeseries",
}


@dataclass
class DashboardPanel:
    id: str
    title: str
    # One of: timeseries | stat | table | logs
    type: str
    # prometheus | loki | "" (unknown / mixed)
    datasource: str
    # The PromQL / LogQL expression Daalu runs to fill this panel.
    expr: str
    unit: str | None = None
    # Layout hint from Grafana's gridPos so the UI can roughly mirror the
    # original arrangement (24-col grid).
    w: int = 12
    h: int = 8
    x: int = 0
    y: int = 0


@dataclass
class DashboardSpec:
    uid: str
    title: str
    description: str
    tags: list[str]
    source: str  # always "grafana" today
    panels: list[DashboardPanel] = field(default_factory=list)


@dataclass
class DashboardSummary:
    uid: str
    title: str
    folder: str | None
    tags: list[str]
    source: str = "grafana"


# ── connection resolution ────────────────────────────────────────────────


@dataclass
class _Conn:
    base_url: str
    token: str | None
    proxy: str | None
    verify_tls: bool


async def _resolve(tenant_id: uuid.UUID) -> _Conn:
    """Load the tenant's grafana Integration row → connection params.

    Raises :class:`GrafanaUnavailable` when no row exists or it has no URL.
    """
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == "grafana",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise GrafanaUnavailable(
                "no Grafana integration configured for this tenant — add a "
                "`grafana` integration with `url` (+ optional api_token) set."
            )
        cfg = row.config or {}
        base_url = (cfg.get("url") or "").rstrip("/")
        if not base_url:
            raise GrafanaUnavailable("grafana integration has no `url`")
        token = cfg.get("api_token") or cfg.get("token") or cfg.get("bearer_token")
        proxy = await get_proxy_url(db, row.cluster_tunnel_id)
    return _Conn(
        base_url=base_url,
        token=str(token) if token else None,
        proxy=proxy,
        verify_tls=bool(cfg.get("verify_tls", True)),
    )


def _client(conn: _Conn) -> httpx.AsyncClient:
    headers = {"Accept": "application/json"}
    if conn.token:
        headers["Authorization"] = f"Bearer {conn.token}"
    return httpx.AsyncClient(
        base_url=conn.base_url,
        headers=headers,
        timeout=_HTTP_TIMEOUT_S,
        proxy=conn.proxy,
        verify=conn.verify_tls,
    )


# ── API surface ──────────────────────────────────────────────────────────


async def health(tenant_id: uuid.UUID) -> tuple[bool, str]:
    """Probe the tenant's Grafana for the integration-health beat task.

    Hits ``/api/search?limit=1`` — a 200 proves both reachability (through the
    edge proxy when tunnel-bound) *and* that the API token authenticates, which
    is exactly what the dashboards data plane needs. Returns ``(ok, detail)``.
    """
    try:
        conn = await _resolve(tenant_id)
    except GrafanaUnavailable as e:
        return False, str(e)
    async with _client(conn) as c:
        try:
            r = await c.get("/api/search", params={"type": "dash-db", "limit": 1})
            r.raise_for_status()
        except httpx.HTTPError as e:
            detail = ""
            if isinstance(e, httpx.HTTPStatusError):
                detail = f" (HTTP {e.response.status_code})"
            return False, f"Grafana unreachable — {e}{detail}"[:500]
    return True, f"ok ({conn.base_url})"


async def list_dashboards(tenant_id: uuid.UUID) -> list[DashboardSummary]:
    """Enumerate the tenant's Grafana dashboards (search API)."""
    conn = await _resolve(tenant_id)
    async with _client(conn) as c:
        try:
            r = await c.get("/api/search", params={"type": "dash-db", "limit": 500})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise GrafanaUnavailable(f"Grafana search failed — {e}") from e
        rows = r.json()
    out: list[DashboardSummary] = []
    for d in rows:
        uid = d.get("uid")
        if not uid:
            continue
        out.append(
            DashboardSummary(
                uid=uid,
                title=d.get("title") or uid,
                folder=d.get("folderTitle"),
                tags=list(d.get("tags") or []),
            )
        )
    out.sort(key=lambda s: ((s.folder or "").lower(), s.title.lower()))
    return out


async def get_dashboard(tenant_id: uuid.UUID, uid: str) -> DashboardSpec:
    """Fetch one dashboard and translate it to a Daalu render spec."""
    conn = await _resolve(tenant_id)
    async with _client(conn) as c:
        try:
            r = await c.get(f"/api/dashboards/uid/{uid}")
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise GrafanaUnavailable(f"Grafana dashboard fetch failed — {e}") from e
        body = r.json()
    return translate_dashboard(body.get("dashboard") or {})


async def create_dashboard(
    tenant_id: uuid.UUID, model: dict[str, Any], *, folder_uid: str | None = None
) -> dict[str, Any]:
    """Create (or overwrite) a dashboard in Grafana from a full dashboard model.

    Returns the Grafana response ``{uid, url, status, ...}``. The dashboard
    then appears in :func:`list_dashboards` like any other.
    """
    conn = await _resolve(tenant_id)
    payload: dict[str, Any] = {
        "dashboard": {**model, "id": None},
        "overwrite": False,
        "message": "Created via Daalu AI dashboard builder",
    }
    if folder_uid:
        payload["folderUid"] = folder_uid
    async with _client(conn) as c:
        try:
            r = await c.post("/api/dashboards/db", json=payload)
            r.raise_for_status()
        except httpx.HTTPError as e:
            detail = ""
            if isinstance(e, httpx.HTTPStatusError):
                detail = f" ({e.response.status_code}: {e.response.text[:300]})"
            raise GrafanaUnavailable(
                f"Grafana dashboard create failed — {e}{detail}"
            ) from e
        return r.json()


# ── translation ──────────────────────────────────────────────────────────


def _datasource_kind(panel: dict[str, Any], target: dict[str, Any]) -> str:
    """Best-effort: is this panel/target backed by Prometheus or Loki?

    Grafana's ``datasource`` field is a ``{type, uid}`` object on modern
    schemas and a bare name string on older ones. We classify by the type
    token, defaulting to prometheus when the expr looks like PromQL.
    """
    for ds in (target.get("datasource"), panel.get("datasource")):
        if isinstance(ds, dict):
            t = (ds.get("type") or "").lower()
            if "loki" in t:
                return "loki"
            if "prometheus" in t or "thanos" in t or "mimir" in t:
                return "prometheus"
        elif isinstance(ds, str):
            t = ds.lower()
            if "loki" in t:
                return "loki"
            if "prom" in t or "thanos" in t or "mimir" in t:
                return "prometheus"
    # Heuristic fallback: a LogQL stream selector usually starts with `{`.
    expr = (target.get("expr") or "").strip()
    if expr.startswith("{") and "|" in expr:
        return "loki"
    return "prometheus"


def translate_dashboard(dash: dict[str, Any]) -> DashboardSpec:
    """Translate a Grafana dashboard model into a flat Daalu render spec.

    We keep only what the native renderer needs: each panel's title, render
    type, datasource kind, and its first runnable target expression. Rows are
    flattened (their nested panels are promoted); panels without a usable
    expression are dropped (text panels, etc.).
    """
    panels_out: list[DashboardPanel] = []

    def _consume(panel: dict[str, Any]) -> None:
        ptype = (panel.get("type") or "").lower()
        if ptype == "row":
            for nested in panel.get("panels") or []:
                _consume(nested)
            return
        targets = panel.get("targets") or []
        target = next(
            (t for t in targets if (t.get("expr") or "").strip()), None
        )
        if target is None:
            return  # nothing to render (text/dashlist/news/etc.)
        grid = panel.get("gridPos") or {}
        unit = (
            ((panel.get("fieldConfig") or {}).get("defaults") or {}).get("unit")
        )
        panels_out.append(
            DashboardPanel(
                id=str(panel.get("id", len(panels_out))),
                title=panel.get("title") or "(untitled)",
                type=_PANEL_TYPE_MAP.get(ptype, "timeseries"),
                datasource=_datasource_kind(panel, target),
                expr=target["expr"].strip(),
                unit=unit or None,
                w=int(grid.get("w", 12)),
                h=int(grid.get("h", 8)),
                x=int(grid.get("x", 0)),
                y=int(grid.get("y", 0)),
            )
        )

    for panel in dash.get("panels") or []:
        _consume(panel)

    return DashboardSpec(
        uid=dash.get("uid") or "",
        title=dash.get("title") or "Dashboard",
        description=dash.get("description") or "",
        tags=list(dash.get("tags") or []),
        source="grafana",
        panels=panels_out,
    )
