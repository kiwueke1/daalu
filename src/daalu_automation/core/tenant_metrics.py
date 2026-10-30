"""Per-tenant metric/log query data plane for the native dashboard renderer.

The dashboards feature reads panel *definitions* from Grafana
(:mod:`daalu_automation.core.grafana`) but fills them by querying the tenant's
own Prometheus/Thanos/Loki — never Grafana — so charts render natively in the
Daalu UI. This module is that query layer.

It resolves the same per-tenant ``Integration`` rows the alert-chat tools use
(``thanos`` preferred over ``prometheus`` for PromQL; ``loki`` for logs) and
dials them through the daalu-edge forward proxy when the row is bound to a
cluster tunnel. Results come back as plain dataclasses the API serialises
straight to the chart components.
"""

from __future__ import annotations

import base64
import re
import uuid
from dataclasses import dataclass, field

import httpx
import structlog
from sqlalchemy import select

from daalu_automation.config import get_settings
from daalu_automation.core.cluster_proxy import get_proxy_url
from daalu_automation.core.tenant_settings import get_tenant_config
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import Integration

logger = structlog.get_logger(__name__)


class MetricsUnavailable(RuntimeError):
    """No metric/log store configured for the tenant, or it is unreachable."""


_HTTP_TIMEOUT_S = 20


@dataclass
class SeriesPoint:
    ts: int
    value: float


@dataclass
class Series:
    # A compact label-set name, e.g. {pod="checkout-1"} → "checkout-1" or the
    # full label string when no obvious single label stands out.
    name: str
    points: list[SeriesPoint] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class InstantValue:
    name: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class LogLine:
    ts: int  # unix ms
    line: str
    labels: dict[str, str] = field(default_factory=dict)


# ── connection resolution ────────────────────────────────────────────────


@dataclass
class _PromConn:
    base_url: str
    proxy: str | None


@dataclass
class _LokiConn:
    base_url: str
    auth: str | None
    proxy: str | None


async def _prometheus_conn(tenant_id: uuid.UUID) -> _PromConn:
    """Resolve the PromQL endpoint that backs the tenant's dashboards.

    Dashboards are read from a specific Grafana, and a Grafana's panels query
    *that Grafana's* datasource — so we fill them from the Prometheus
    co-located with the grafana integration, keyed off its cluster-tunnel
    binding:

    * **Grafana is hub-local** (no ``cluster_tunnel_id``) → the hub's own
      in-cluster Prometheus (``settings.prometheus_base_url`` — the proven
      store the AI-factory panels use). We deliberately ignore the tenant's
      tunnel-bound ``thanos``/``prometheus`` rows here: those describe a
      *remote* workload cluster (and the hub's thanos-query is store-less), not
      the hub the dashboards are about.
    * **Grafana is tunnel-bound** (a customer's own remote Grafana) → the
      ``prometheus``/``thanos`` row on the *same* tunnel, dialed through the
      edge proxy. Prometheus is preferred over Thanos (Thanos is frequently
      store-less in these stacks).

    Falls back to any integration row URL, then the tenant-config default.
    """
    settings = get_settings()
    async with AsyncSessionLocal() as db:
        grafana_row = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == "grafana",
                )
            )
        ).scalar_one_or_none()
        prom = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == "prometheus",
                )
            )
        ).scalar_one_or_none()
        thanos = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == "thanos",
                )
            )
        ).scalar_one_or_none()

        ctx_tunnel = grafana_row.cluster_tunnel_id if grafana_row else None

        # Grafana lives on a remote cluster → query the metric store on the
        # same tunnel (prefer prometheus over the often-empty thanos).
        if ctx_tunnel is not None:
            same = next(
                (
                    r
                    for r in (prom, thanos)
                    if r
                    and r.cluster_tunnel_id == ctx_tunnel
                    and (r.config or {}).get("url")
                ),
                None,
            )
            if same is not None:
                proxy = await get_proxy_url(db, ctx_tunnel)
                return _PromConn(
                    base_url=str(same.config["url"]).rstrip("/"), proxy=proxy
                )

        # Hub-local Grafana (or no co-located row) → the hub Prometheus.
        env_url = (getattr(settings, "prometheus_base_url", "") or "").rstrip("/")
        if env_url:
            return _PromConn(base_url=env_url, proxy=None)

        # Last resort: any configured row URL, then the tenant-config default.
        chosen = prom or thanos
        url = (chosen.config or {}).get("url") if chosen else ""
        if not url:
            cfg = await get_tenant_config(db, tenant_id)
            url = cfg.prometheus_url
    if not url:
        raise MetricsUnavailable(
            "no Prometheus/Thanos endpoint configured for this tenant"
        )
    return _PromConn(base_url=url.rstrip("/"), proxy=None)


async def _loki_conn(tenant_id: uuid.UUID) -> _LokiConn:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == tenant_id,
                    Integration.provider == "loki",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise MetricsUnavailable("no Loki integration configured for this tenant")
        cfg = row.config or {}
        url = (cfg.get("url") or "").rstrip("/")
        if not url:
            raise MetricsUnavailable("loki integration has no `url`")
        auth = _basic_auth(cfg)
        proxy = await get_proxy_url(db, row.cluster_tunnel_id)
    return _LokiConn(base_url=url, auth=auth, proxy=proxy)


def _basic_auth(cfg: dict) -> str | None:
    if cfg.get("auth_header"):
        return str(cfg["auth_header"])
    token = cfg.get("bearer_token") or cfg.get("api_token")
    if token:
        return f"Bearer {token}"
    user, pwd = cfg.get("user") or cfg.get("username"), cfg.get("password")
    if user and pwd:
        return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return None


# ── label naming ─────────────────────────────────────────────────────────


# ── Grafana template-variable expansion ──────────────────────────────────


# Grafana dashboards parameterise queries with template variables (``$job``,
# ``$cluster``) and built-in macros (``$__rate_interval``). Daalu renders the
# raw expression with no variable picker, so we expand them to sensible
# wildcards/defaults: most kube dashboards use ``label=~"$var"`` matchers, where
# ``.*`` means "all values" — exactly the aggregated view we want by default.
_INTERVAL_MACROS = re.compile(r"\$__(rate_interval|interval|interval_ms)")
_RANGE_MACROS = re.compile(r"\$__(range_s|range_ms|range)")
_VAR_BRACED = re.compile(r"\$\{[A-Za-z0-9_]+(?::[A-Za-z0-9_]+)?\}")
_VAR_BARE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
# An exact (``=``/``!=``) label matcher whose value contains a template var,
# e.g. ``namespace="$namespace"``. We flip these to regex (``=~``/``!~``) so the
# ``.*`` wildcard we substitute below actually means "all values" instead of an
# exact match for the literal string ``.*``. ``=~``/``!~`` matchers are left as
# they are (their leading char isn't ``=``/``!=`` followed by a quote).
_EXACT_VAR_MATCHER = re.compile(
    r'([A-Za-z_][A-Za-z0-9_]*\s*)(!?=)(\s*"[^"]*\$[^"]*")'
)


def expand_template_vars(expr: str) -> str:
    """Best-effort expansion of Grafana template vars/macros to runnable PromQL.

    Not a full Grafana variable engine — it makes templated panels return the
    all-values aggregate instead of erroring on an unresolved ``$var``.
    """
    if "$" not in expr:
        return expr
    out = _INTERVAL_MACROS.sub("5m", expr)
    out = _RANGE_MACROS.sub("1h", out)
    # Drop a datasource selector entirely (it's not part of the query body).
    out = out.replace('"$datasource"', '""').replace("${datasource}", "")
    # Promote ``label="$var"`` → ``label=~"$var"`` so the wildcard below matches
    # all series (and absent labels) rather than a literal ``.*`` (``!=`` → the
    # negative-regex ``!~``, not the invalid ``!=~``).
    out = _EXACT_VAR_MATCHER.sub(
        lambda m: f"{m.group(1)}{'=~' if m.group(2) == '=' else '!~'}{m.group(3)}",
        out,
    )
    out = _VAR_BRACED.sub(".*", out)
    out = _VAR_BARE.sub(".*", out)
    return out


def _series_name(metric: dict[str, str]) -> str:
    """A short, human label for a series legend, chosen from the most
    identifying label present (pod / instance / node / app / __name__)."""
    for key in ("pod", "instance", "node", "app", "container", "namespace", "job"):
        if metric.get(key):
            return metric[key]
    name = metric.get("__name__", "")
    rest = {k: v for k, v in metric.items() if k != "__name__"}
    if not rest:
        return name or "value"
    inner = ",".join(f"{k}={v}" for k, v in sorted(rest.items()))
    return f"{name}{{{inner}}}" if name else f"{{{inner}}}"


# ── PromQL ───────────────────────────────────────────────────────────────


async def query_range(
    tenant_id: uuid.UUID, promql: str, *, start: float, end: float, step_s: int
) -> list[Series]:
    """Run a PromQL range query → one :class:`Series` per returned timeseries."""
    conn = await _prometheus_conn(tenant_id)
    promql = expand_template_vars(promql)
    params = {"query": promql, "start": start, "end": end, "step": max(1, step_s)}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, proxy=conn.proxy) as c:
        try:
            r = await c.get(f"{conn.base_url}/api/v1/query_range", params=params)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise MetricsUnavailable(f"prometheus query failed — {e}") from e
        data = r.json()
    if data.get("status") != "success":
        raise MetricsUnavailable(f"prometheus error: {data.get('error') or data}")
    out: list[Series] = []
    for s in data.get("data", {}).get("result", []):
        metric = s.get("metric", {}) or {}
        pts = [
            SeriesPoint(ts=int(float(ts)), value=_to_float(v))
            for ts, v in s.get("values", [])
        ]
        out.append(Series(name=_series_name(metric), points=pts, labels=metric))
    return out


async def query_instant(tenant_id: uuid.UUID, promql: str) -> list[InstantValue]:
    """Run an instant PromQL query → one value per returned series."""
    conn = await _prometheus_conn(tenant_id)
    promql = expand_template_vars(promql)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, proxy=conn.proxy) as c:
        try:
            r = await c.get(
                f"{conn.base_url}/api/v1/query", params={"query": promql}
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise MetricsUnavailable(f"prometheus query failed — {e}") from e
        data = r.json()
    if data.get("status") != "success":
        raise MetricsUnavailable(f"prometheus error: {data.get('error') or data}")
    out: list[InstantValue] = []
    for s in data.get("data", {}).get("result", []):
        metric = s.get("metric", {}) or {}
        value = s.get("value") or [0, "0"]
        out.append(
            InstantValue(
                name=_series_name(metric), value=_to_float(value[1]), labels=metric
            )
        )
    return out


# ── LogQL ────────────────────────────────────────────────────────────────


async def query_logs(
    tenant_id: uuid.UUID, logql: str, *, start: float, end: float, limit: int = 200
) -> list[LogLine]:
    """Run a LogQL range query → newest-first log lines."""
    conn = await _loki_conn(tenant_id)
    logql = expand_template_vars(logql)
    params = {
        "query": logql,
        "start": int(start * 1e9),
        "end": int(end * 1e9),
        "limit": limit,
        "direction": "backward",
    }
    headers = {"Authorization": conn.auth} if conn.auth else {}
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_S, proxy=conn.proxy, headers=headers
    ) as c:
        try:
            r = await c.get(f"{conn.base_url}/loki/api/v1/query_range", params=params)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise MetricsUnavailable(f"loki query failed — {e}") from e
        data = r.json()
    out: list[LogLine] = []
    for stream in data.get("data", {}).get("result", []):
        labels = stream.get("stream", {}) or {}
        for ts_ns, line in stream.get("values", []):
            out.append(
                LogLine(ts=int(int(ts_ns) // 1_000_000), line=line, labels=labels)
            )
    out.sort(key=lambda x: x.ts, reverse=True)
    return out[:limit]


def _to_float(v: object) -> float:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    # Prometheus encodes NaN/Inf as strings → coerce to 0 for charting.
    if f != f or f in (float("inf"), float("-inf")):
        return 0.0
    return f
