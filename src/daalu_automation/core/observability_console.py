"""Read-only observability console for Prometheus/Thanos (metrics) and Loki (logs).

Backs the per-provider detail page under ``/observability/{provider}`` in the
UI — the metrics/logs analogue of the kubectl console
(:mod:`daalu_automation.core.kube_console`). Each backing store the operator
has connected as an ``Integration`` row gets:

* **Overview** — headline health/inventory read straight from the store's HTTP
  API (Prometheus/Thanos: scrape-target up/down counts, firing alerts, build
  version, per-job target health; Loki: label inventory + namespaces seen).
* **A curated query runner** — a fixed allowlist of read-only PromQL/LogQL
  *panels*. The operator ticks one or more, picks a time window, and each runs
  against the store and returns rendered output.

Why an allowlist of named panels rather than raw PromQL/LogQL: same rationale
as :mod:`kube_console` — the only inputs the caller controls are a panel *id*
(validated against the catalog), a time window (validated), and — for logs — a
namespace and a substring filter (both regex/charset-checked and quote-escaped
before they reach LogQL). Every panel resolves to a single ``GET`` against the
store's query API; nothing here can mutate state. A power user may also pass one
free-form PromQL expression (``custom_query``) which is still only ever read.

Endpoint + auth + edge-proxy resolution reuses the same ``Integration``-row
shape the agent's ``query_prometheus`` / ``query_loki`` tools use
(:mod:`daalu_automation.core.kube_tools`).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from sqlalchemy import select

from daalu_automation.core.cluster_proxy import get_proxy_url
from daalu_automation.core.kube_tools import (
    _auth_header_from_integration,
    _duration_to_seconds,
)
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import ClusterTunnel, Integration

logger = structlog.get_logger(__name__)


class ObsConsoleError(RuntimeError):
    """An observability query could not be resolved or executed."""


# Which providers this console understands, and which family they belong to.
METRICS_PROVIDERS = ("prometheus", "thanos")
LOGS_PROVIDERS = ("loki", "opensearch")

# Time windows offered for metric panels (``instant`` → /api/v1/query).
METRIC_WINDOWS = ("instant", "5m", "15m", "1h", "6h", "24h")
# ``since`` windows offered for log panels.
LOG_WINDOWS = ("5m", "15m", "1h", "6h", "24h")

_MAX_PANELS_PER_RUN = 25
_MAX_SERIES_RENDERED = 60
_HTTP_TIMEOUT_S = 20.0

# Free-form log inputs are validated before they're spliced into LogQL.
_NS_RE = re.compile(r"^[A-Za-z0-9_.*+|()\-\[\]]{0,128}$")
_SEARCH_RE = re.compile(r"^[^\n\r]{0,200}$")


def family_for(provider: str) -> str | None:
    if provider in METRICS_PROVIDERS:
        return "metrics"
    if provider in LOGS_PROVIDERS:
        return "logs"
    return None


# ── panel catalogs ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MetricPanel:
    id: str
    label: str
    group: str
    promql: str
    description: str = ""
    unit: str = ""


@dataclass(frozen=True)
class LogPanel:
    id: str
    label: str
    group: str
    description: str = ""
    # Extra LogQL line filter appended after the stream selector,
    # e.g. ``(?i)error|panic`` becomes ``... |~ "(?i)error|panic"``.
    line_filter: str | None = None
    # Explicit stream selector. Most panels leave this None and get the
    # namespace-scoped default — but non-Kubernetes streams (network-device
    # syslog has no ``namespace`` label) need their own selector, and the
    # namespace filter is deliberately not applied to them.
    stream: str | None = None


# A pragmatic default set for a kube-prometheus-stack cluster (node-exporter +
# kube-state-metrics + cAdvisor) plus the NVIDIA DCGM exporter. Panels that
# don't match anything on a given install simply return "(no series)".
METRIC_PANELS: list[MetricPanel] = [
    # ── Health ───────────────────────────────────────────────────────────
    MetricPanel("targets-up", "Scrape targets up", "Health", "count(up == 1)",
                "How many scrape targets are currently healthy.", "targets"),
    MetricPanel("targets-down", "Scrape targets down", "Health", "up == 0",
                "Each down target (job/instance) Prometheus can't scrape."),
    MetricPanel("targets-by-job", "Targets by job", "Health",
                "count by (job) (up)", "Target count per scrape job."),
    MetricPanel("firing-alerts", "Firing alerts", "Health",
                'ALERTS{alertstate="firing"}',
                "Alert rules currently firing (with their labels)."),
    MetricPanel("build-info", "Prometheus build", "Health",
                "prometheus_build_info", "Version of the Prometheus server."),
    # ── Nodes ────────────────────────────────────────────────────────────
    MetricPanel("node-cpu", "Node CPU usage %", "Nodes",
                '100 - (avg by (instance) '
                '(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
                "Per-node CPU utilisation.", "%"),
    MetricPanel("node-mem", "Node memory usage %", "Nodes",
                "100 * (1 - node_memory_MemAvailable_bytes "
                "/ node_memory_MemTotal_bytes)",
                "Per-node memory utilisation.", "%"),
    MetricPanel("node-fs", "Filesystem usage %", "Nodes",
                '100 - (node_filesystem_avail_bytes'
                '{fstype!~"tmpfs|overlay|squashfs|ramfs"} '
                "/ node_filesystem_size_bytes * 100)",
                "Per-mountpoint disk utilisation.", "%"),
    MetricPanel("node-load", "Node load (1m)", "Nodes", "node_load1",
                "1-minute load average per node."),
    # ── Workloads ────────────────────────────────────────────────────────
    MetricPanel("pods-by-ns", "Pods per namespace", "Workloads",
                "count by (namespace) (kube_pod_info)",
                "Pod count grouped by namespace."),
    MetricPanel("pods-not-running", "Pods not running", "Workloads",
                'kube_pod_status_phase{phase=~"Pending|Failed|Unknown"} == 1',
                "Pods stuck in a non-running phase."),
    MetricPanel("restarts", "Top container restarts", "Workloads",
                "topk(15, sum by (namespace, pod) "
                "(kube_pod_container_status_restarts_total))",
                "Containers with the most restarts."),
    MetricPanel("waiting", "Containers waiting", "Workloads",
                "sum by (namespace, reason) "
                "(kube_pod_container_status_waiting_reason == 1)",
                "Waiting containers grouped by reason "
                "(CrashLoopBackOff, ImagePullBackOff, …)."),
    MetricPanel("cpu-by-ns", "CPU cores by namespace", "Workloads",
                'sum by (namespace) '
                '(rate(container_cpu_usage_seconds_total{container!=""}[5m]))',
                "Live CPU consumption per namespace.", "cores"),
    MetricPanel("mem-by-ns", "Memory by namespace", "Workloads",
                'sum by (namespace) '
                '(container_memory_working_set_bytes{container!=""})',
                "Working-set memory per namespace.", "bytes"),
    # ── GPU (NVIDIA DCGM exporter) ─────────────────────────────────────────
    MetricPanel("gpu-temp", "GPU temperature", "GPU",
                "DCGM_FI_DEV_GPU_TEMP", "Per-GPU temperature.", "°C"),
    MetricPanel("gpu-util", "GPU utilisation", "GPU",
                "DCGM_FI_DEV_GPU_UTIL", "Per-GPU SM utilisation.", "%"),
    MetricPanel("gpu-mem-used", "GPU memory used", "GPU",
                "DCGM_FI_DEV_FB_USED", "Framebuffer memory in use.", "MiB"),
    MetricPanel("gpu-power", "GPU power draw", "GPU",
                "DCGM_FI_DEV_POWER_USAGE", "Per-GPU power consumption.", "W"),
    # ── Network devices (node_exporter on switches/servers, job set by the
    #    daalu-network-devices ScrapeConfig; `device` label = SoT name) ──────
    MetricPanel("net-up", "Network devices up", "Network",
                'up{job="network-devices"}',
                "Per-device scrape health for the fabric.", ""),
    MetricPanel("net-cpu", "Device CPU usage %", "Network",
                '100 * (1 - avg by (device) '
                '(rate(node_cpu_seconds_total{job="network-devices",mode="idle"}[5m])))',
                "CPU utilisation per network device.", "%"),
    MetricPanel("net-mem", "Device memory usage %", "Network",
                '100 * (1 - node_memory_MemAvailable_bytes{job="network-devices"} '
                '/ node_memory_MemTotal_bytes{job="network-devices"})',
                "Memory utilisation per network device.", "%"),
    MetricPanel("net-load", "Device load (1m)", "Network",
                'node_load1{job="network-devices"}',
                "1-minute load average per device.", ""),
    MetricPanel("net-traffic", "Device network throughput", "Network",
                'sum by (device) '
                '(rate(node_network_receive_bytes_total{job="network-devices"}[5m]) '
                '+ rate(node_network_transmit_bytes_total{job="network-devices"}[5m]))',
                "Total rx+tx bytes/s across each device's interfaces.", "B/s"),
]

LOG_PANELS: list[LogPanel] = [
    LogPanel("recent", "Recent logs", "Explore",
             "Every line from the matched streams, newest first."),
    LogPanel("errors", "Errors & exceptions", "Explore",
             "Lines mentioning error/exception/traceback/fatal/panic.",
             r"(?i)error|exception|traceback|fatal|panic"),
    LogPanel("warnings", "Warnings", "Explore",
             "Lines mentioning warn/warning.", r"(?i)warn"),
    LogPanel("oom", "OOM / killed", "Health",
             "Out-of-memory kills and killed processes.",
             r"(?i)oom|out of memory|killed|cannot allocate"),
    LogPanel("crash", "Crashes & restarts", "Health",
             "CrashLoopBackOff / segfault / restart markers.",
             r"(?i)crashloop|segfault|sigsegv|sigkill|restart"),
    LogPanel("http-5xx", "HTTP 5xx", "Traffic",
             "Lines that look like a 5xx HTTP status.",
             r"\b5[0-9][0-9]\b"),
    LogPanel("timeouts", "Timeouts & refused", "Traffic",
             "Connection timeouts / refused / reset.",
             r"(?i)timeout|timed out|connection refused|connection reset"),
    # ── Network devices (syslog shipped off the fabric; `device` label =
    #    SoT name; not namespace-scoped) ────────────────────────────────────
    LogPanel("net-syslog", "Network device syslog", "Network",
             "Everything the fabric devices log, newest first.",
             None, '{job="network-syslog"}'),
    LogPanel("net-errors", "Network device errors", "Network",
             "Device log lines mentioning error/fail/down.",
             r"(?i)error|fail|down|crit", '{job="network-syslog"}'),
]

_METRIC_BY_ID = {p.id: p for p in METRIC_PANELS}
_LOG_BY_ID = {p.id: p for p in LOG_PANELS}


def metrics_catalog() -> list[MetricPanel]:
    return list(METRIC_PANELS)


def logs_catalog(provider: str = "loki") -> list[LogPanel]:
    if provider == "opensearch":
        return list(OPENSEARCH_PANELS)
    return list(LOG_PANELS)


# OpenSearch panels — query-string searches over the fluent-bit indices
# (kubernetes-*). Same LogPanel shape; `line_filter` holds the Lucene
# query_string instead of a LogQL regex; `stream` is unused.
OPENSEARCH_PANELS: list[LogPanel] = [
    LogPanel("os-recent", "Recent logs", "Explore",
             "Newest documents across the kubernetes-* indices."),
    LogPanel("os-errors", "Errors & exceptions", "Explore",
             "Documents matching error/exception/fatal/panic.",
             "error OR exception OR traceback OR fatal OR panic"),
    LogPanel("os-warnings", "Warnings", "Explore",
             "Documents matching warn/warning.", "warn OR warning"),
    LogPanel("os-oom", "OOM / killed", "Health",
             "Out-of-memory kills and killed processes.",
             '"out of memory" OR oom OR killed'),
    LogPanel("os-crash", "Crashes & restarts", "Health",
             "CrashLoop / segfault / restart markers.",
             "crashloop OR segfault OR sigsegv OR sigkill OR restart"),
]


def log_display_query(panel: LogPanel) -> str:
    """The LogQL a panel runs with the default (all-namespace) filter — shown
    in the UI under each checkbox, mirroring the kubectl command preview."""
    if panel.id.startswith("os-"):
        # OpenSearch panels: line_filter is a Lucene query_string, not LogQL.
        return panel.line_filter or "*"
    q = panel.stream or '{namespace=~".+"}'
    if panel.line_filter:
        q += f' |~ "{panel.line_filter}"'
    return q


# ── endpoint resolution ────────────────────────────────────────────────────


async def _resolve(
    provider: str, tenant_id: uuid.UUID | None, cluster: str | None = None
) -> tuple[str, str | None, str | None]:
    """Return ``(base_url, auth_header, proxy_url)`` for a connected store.

    Mirrors the integration-row lookup the agent's query tools use: the
    ``url`` + optional credential live on the ``Integration`` row, and a
    ``cluster_tunnel_id`` (if set) routes the call through the edge proxy.

    ``cluster`` (a ClusterTunnel slug) scopes the lookup to the store that
    belongs to that cluster, so a cluster page never shows another
    cluster's metrics or logs. Without it, the tenant's store is used.
    """
    if tenant_id is None:
        raise ObsConsoleError("tenant context missing")
    async with AsyncSessionLocal() as db:
        stmt = select(Integration).where(
            Integration.tenant_id == tenant_id,
            Integration.provider == provider,
        )
        if cluster:
            tunnel_id = (
                await db.execute(
                    select(ClusterTunnel.id).where(
                        ClusterTunnel.tenant_id == tenant_id,
                        ClusterTunnel.slug == cluster,
                    )
                )
            ).scalar_one_or_none()
            if tunnel_id is None:
                raise ObsConsoleError(f"cluster {cluster!r} not found")
            stmt = stmt.where(Integration.cluster_tunnel_id == tunnel_id)
        row = (
            await db.execute(stmt.order_by(Integration.created_at).limit(1))
        ).scalar_one_or_none()
        if row is None:
            raise ObsConsoleError(
                f"no {provider!r} integration for cluster {cluster!r}"
                if cluster
                else f"no {provider!r} integration configured for this tenant"
            )
        config = row.config or {}
        proxy = await get_proxy_url(db, row.cluster_tunnel_id)
    base = (config.get("url") or "").rstrip("/")
    if not base:
        raise ObsConsoleError(f"{provider!r} integration has no `url` set")
    return base, _auth_header_from_integration(config), proxy


def _headers(auth: str | None) -> dict[str, str]:
    # `Accept-Encoding: identity` is deliberate: when the store is reached
    # through the WireGuard edge HTTP proxy, a gzip'd response can come back
    # with a wrong Content-Length and httpx then truncates the body mid-stream
    # (JSONDecodeError) or stalls. These are small JSON payloads, so asking the
    # store not to compress sidesteps the proxy bug at no real cost.
    h = {"Accept-Encoding": "identity"}
    if auth:
        h["Authorization"] = auth
    return h


# ── Prometheus / Thanos ────────────────────────────────────────────────────


async def _prom_query(
    base: str,
    auth: str | None,
    proxy: str | None,
    promql: str,
    *,
    time_range: str = "instant",
) -> dict[str, Any]:
    """Run an instant or range PromQL query and return the ``data`` object
    (``{resultType, result}``). Raises :class:`ObsConsoleError` on a Prometheus
    error envelope."""
    params: dict[str, Any] = {"query": promql}
    if time_range and time_range != "instant":
        seconds = _duration_to_seconds(time_range)
        if seconds is None:
            raise ObsConsoleError(f"bad time range {time_range!r}")
        end = datetime.now(tz=timezone.utc).timestamp()
        params.update({"start": end - seconds, "end": end,
                       "step": max(15, seconds // 60)})
        url = f"{base}/api/v1/query_range"
    else:
        url = f"{base}/api/v1/query"
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_S, proxy=proxy, headers=_headers(auth)
    ) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    if data.get("status") != "success":
        raise ObsConsoleError(
            f"prometheus: {data.get('error') or data.get('status')}"
        )
    return data.get("data", {})


async def _prom_scalar(
    base: str, auth: str | None, proxy: str | None, promql: str
) -> float | None:
    data = await _prom_query(base, auth, proxy, promql)
    result = data.get("result") or []
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, ValueError, TypeError):
        return None


async def _prom_vector(
    base: str, auth: str | None, proxy: str | None, promql: str, label: str
) -> dict[str, float]:
    """Return ``{label_value: numeric_value}`` for a ``by (label)`` vector."""
    data = await _prom_query(base, auth, proxy, promql)
    out: dict[str, float] = {}
    for row in data.get("result") or []:
        key = (row.get("metric") or {}).get(label, "")
        try:
            out[key] = float(row["value"][1])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return out


@dataclass
class JobHealth:
    job: str
    up: int
    total: int


@dataclass
class MetricsOverview:
    reachable: bool
    base_url: str = ""
    version: str | None = None
    targets_total: int | None = None
    targets_up: int | None = None
    targets_down: int | None = None
    firing_alerts: int | None = None
    jobs: list[JobHealth] = field(default_factory=list)
    error: str | None = None


async def metrics_overview(
    provider: str, tenant_id: uuid.UUID | None, cluster: str | None = None
) -> MetricsOverview:
    try:
        base, auth, proxy = await _resolve(provider, tenant_id, cluster)
    except ObsConsoleError as e:
        return MetricsOverview(reachable=False, error=str(e))

    ov = MetricsOverview(reachable=True, base_url=base)
    # First query doubles as the reachability probe.
    try:
        ov.targets_total = _as_int(await _prom_scalar(base, auth, proxy, "count(up)"))
    except Exception as e:  # noqa: BLE001
        logger.warning("obs.metrics_overview_unreachable", provider=provider,
                       error=str(e))
        return MetricsOverview(reachable=False, base_url=base,
                               error=_humanise(e))

    async def _safe_scalar(promql: str) -> int | None:
        try:
            return _as_int(await _prom_scalar(base, auth, proxy, promql))
        except Exception:  # noqa: BLE001
            return None

    ov.targets_down = await _safe_scalar("count(up == 0)") or 0
    if ov.targets_total is not None:
        ov.targets_up = max(0, ov.targets_total - (ov.targets_down or 0))
    ov.firing_alerts = (
        await _safe_scalar('count(ALERTS{alertstate="firing"})') or 0
    )
    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_S, proxy=proxy, headers=_headers(auth)
        ) as client:
            r = await client.get(f"{base}/api/v1/status/buildinfo")
            if r.status_code == 200:
                ov.version = (r.json().get("data") or {}).get("version")
    except Exception:  # noqa: BLE001
        pass
    try:
        total_by_job = await _prom_vector(base, auth, proxy,
                                          "count by (job) (up)", "job")
        up_by_job = await _prom_vector(base, auth, proxy,
                                       "sum by (job) (up)", "job")
        ov.jobs = sorted(
            (
                JobHealth(job=job or "(none)", up=int(up_by_job.get(job, 0)),
                          total=int(total))
                for job, total in total_by_job.items()
            ),
            key=lambda j: (j.total - j.up == 0, j.job),
        )
    except Exception:  # noqa: BLE001
        pass
    return ov


# ── Loki ────────────────────────────────────────────────────────────────────


async def _loki_get(
    base: str, auth: str | None, proxy: str | None, path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_S, proxy=proxy, headers=_headers(auth)
    ) as client:
        r = await client.get(f"{base}{path}", params=params or {})
        r.raise_for_status()
        return r.json()


@dataclass
class LogsOverview:
    reachable: bool
    base_url: str = ""
    label_count: int | None = None
    labels: list[str] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    error: str | None = None


async def logs_overview(
    provider: str, tenant_id: uuid.UUID | None, cluster: str | None = None
) -> LogsOverview:
    if provider == "opensearch":
        return await opensearch_overview(tenant_id, cluster)
    try:
        base, auth, proxy = await _resolve(provider, tenant_id, cluster)
    except ObsConsoleError as e:
        return LogsOverview(reachable=False, error=str(e))

    end_ns = int(datetime.now(tz=timezone.utc).timestamp() * 1e9)
    start_ns = end_ns - int(6 * 3600 * 1e9)
    window = {"start": start_ns, "end": end_ns}
    ov = LogsOverview(reachable=True, base_url=base)
    try:
        data = await _loki_get(base, auth, proxy, "/loki/api/v1/labels", window)
    except Exception as e:  # noqa: BLE001
        logger.warning("obs.logs_overview_unreachable", provider=provider,
                       error=str(e))
        return LogsOverview(reachable=False, base_url=base, error=_humanise(e))
    ov.labels = sorted(data.get("data") or [])
    ov.label_count = len(ov.labels)
    if "namespace" in ov.labels:
        try:
            ns = await _loki_get(
                base, auth, proxy,
                "/loki/api/v1/label/namespace/values", window,
            )
            ov.namespaces = sorted(ns.get("data") or [])
        except Exception:  # noqa: BLE001
            pass
    return ov


async def _loki_query_range(
    base: str, auth: str | None, proxy: str | None, logql: str,
    *, since: str, limit: int,
) -> list[dict[str, Any]]:
    seconds = _duration_to_seconds(since)
    if seconds is None:
        raise ObsConsoleError(f"bad since {since!r}")
    end_ns = int(datetime.now(tz=timezone.utc).timestamp() * 1e9)
    start_ns = end_ns - int(seconds * 1e9)
    params = {"query": logql, "start": start_ns, "end": end_ns,
              "limit": limit, "direction": "backward"}
    data = await _loki_get(base, auth, proxy,
                           "/loki/api/v1/query_range", params)
    return data.get("data", {}).get("result", [])


# ── query runner ───────────────────────────────────────────────────────────


@dataclass
class QueryResult:
    id: str
    query: str
    ok: bool
    output: str
    error: str | None = None


async def run_metric_queries(
    provider: str,
    tenant_id: uuid.UUID | None,
    *,
    panel_ids: list[str],
    time_range: str = "instant",
    custom_query: str | None = None,
    actor_id: uuid.UUID | None = None,
    cluster: str | None = None,
) -> list[QueryResult]:
    if time_range not in METRIC_WINDOWS:
        raise ObsConsoleError(f"unknown time range {time_range!r}")
    if len(panel_ids) > _MAX_PANELS_PER_RUN:
        raise ObsConsoleError(f"too many panels (max {_MAX_PANELS_PER_RUN})")

    jobs: list[tuple[str, str]] = []  # (display_id, promql)
    for pid in panel_ids:
        panel = _METRIC_BY_ID.get(pid)
        if panel is None:
            raise ObsConsoleError(f"unknown panel {pid!r}")
        jobs.append((pid, panel.promql))
    if custom_query and custom_query.strip():
        jobs.append(("custom", custom_query.strip()))
    if not jobs:
        raise ObsConsoleError("select at least one panel or enter a query")

    base, auth, proxy = await _resolve(provider, tenant_id, cluster)
    logger.info("obs.run_metrics", provider=provider,
                tenant_id=str(tenant_id) if tenant_id else None,
                actor_id=str(actor_id) if actor_id else None,
                panels=panel_ids, custom=bool(custom_query),
                time_range=time_range)

    results: list[QueryResult] = []
    for pid, promql in jobs:
        try:
            data = await _prom_query(base, auth, proxy, promql,
                                     time_range=time_range)
            results.append(QueryResult(id=pid, query=promql, ok=True,
                                       output=_render_prom(data)))
        except Exception as e:  # noqa: BLE001
            results.append(QueryResult(id=pid, query=promql, ok=False,
                                       output="", error=_humanise(e)))
    return results


async def run_log_queries(
    provider: str,
    tenant_id: uuid.UUID | None,
    *,
    panel_ids: list[str],
    namespace: str | None = None,
    search: str | None = None,
    since: str = "1h",
    limit: int = 200,
    actor_id: uuid.UUID | None = None,
    cluster: str | None = None,
) -> list[QueryResult]:
    if since not in LOG_WINDOWS:
        raise ObsConsoleError(f"unknown window {since!r}")
    if not (1 <= limit <= 1000):
        raise ObsConsoleError("limit must be between 1 and 1000")
    if provider == "opensearch":
        return await _run_opensearch_queries(
            tenant_id, panel_ids=panel_ids, namespace=namespace,
            search=search, since=since, limit=limit, actor_id=actor_id,
            cluster=cluster,
        )
    if len(panel_ids) > _MAX_PANELS_PER_RUN:
        raise ObsConsoleError(f"too many panels (max {_MAX_PANELS_PER_RUN})")
    namespace = (namespace or "").strip()
    if namespace and not _NS_RE.match(namespace):
        raise ObsConsoleError("invalid namespace filter")
    search = (search or "").strip()
    if search and not _SEARCH_RE.match(search):
        raise ObsConsoleError("invalid search filter")
    if not panel_ids:
        raise ObsConsoleError("select at least one panel")

    selector = '{namespace=~"%s"}' % (_escape_logql(namespace) if namespace
                                      else ".+")
    base, auth, proxy = await _resolve(provider, tenant_id, cluster)
    logger.info("obs.run_logs", provider=provider,
                tenant_id=str(tenant_id) if tenant_id else None,
                actor_id=str(actor_id) if actor_id else None,
                panels=panel_ids, namespace=namespace or None,
                search=bool(search), since=since, limit=limit)

    results: list[QueryResult] = []
    for pid in panel_ids:
        panel = _LOG_BY_ID.get(pid)
        if panel is None:
            raise ObsConsoleError(f"unknown panel {pid!r}")
        # Panels with their own stream selector (network syslog) ignore the
        # namespace filter — their streams carry no namespace label.
        logql = panel.stream or selector
        if panel.line_filter:
            logql += f' |~ "{panel.line_filter}"'
        if search:
            logql += f' |~ "{_escape_logql(search)}"'
        try:
            streams = await _loki_query_range(base, auth, proxy, logql,
                                              since=since, limit=limit)
            results.append(QueryResult(id=pid, query=logql, ok=True,
                                       output=_render_loki(streams, limit)))
        except Exception as e:  # noqa: BLE001
            results.append(QueryResult(id=pid, query=logql, ok=False,
                                       output="", error=_humanise(e)))
    return results


# ── rendering ────────────────────────────────────────────────────────────


def _as_int(v: float | None) -> int | None:
    return int(v) if v is not None else None


def _fmt_num(v: Any) -> str:
    try:
        f = float(v)
    except (ValueError, TypeError):
        return str(v)
    if f != f:  # NaN
        return "NaN"
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return f"{f:.4g}"


# Labels that never help identify a series in a console table but bloat it —
# notably the DCGM exporter stamps a dozen of these on every GPU series.
_NOISE_LABELS = frozenset({
    "endpoint", "service", "prometheus", "prometheus_replica",
    "DCGM_FI_DRIVER_VERSION", "container", "id", "image",
})
_MAX_LABEL_LEN = 120


def _series_label(metric: dict[str, str]) -> str:
    labels = {
        k: v for k, v in (metric or {}).items()
        if k != "__name__" and k not in _NOISE_LABELS
    }
    if not labels:
        return (metric or {}).get("__name__", "{}")
    s = "{" + ", ".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}"
    return s if len(s) <= _MAX_LABEL_LEN else s[: _MAX_LABEL_LEN - 2] + "…}"


def _aligned(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))
    fmt = "   ".join(f"{{:<{w}}}" for w in widths)
    out = [fmt.format(*headers).rstrip()]
    for row in rows:
        padded = row + [""] * (len(headers) - len(row))
        out.append(fmt.format(*padded[: len(headers)]).rstrip())
    return "\n".join(out)


def _render_prom(data: dict[str, Any]) -> str:
    rtype = data.get("resultType")
    result = data.get("result") or []
    if rtype == "scalar":
        return _fmt_num(result[1]) if len(result) == 2 else "(empty)"
    if not result:
        return "(no series)"
    truncated = len(result) > _MAX_SERIES_RENDERED
    result = result[:_MAX_SERIES_RENDERED]
    if rtype == "vector":
        rows = [[_series_label(s.get("metric", {})), _fmt_num(s["value"][1])]
                for s in result if s.get("value")]
        body = _aligned(["SERIES", "VALUE"], rows)
    elif rtype == "matrix":
        rows = []
        for s in result:
            vals = [float(v[1]) for v in s.get("values", [])
                    if _is_num(v[1])]
            if not vals:
                continue
            rows.append([
                _series_label(s.get("metric", {})),
                _fmt_num(vals[-1]), _fmt_num(min(vals)),
                _fmt_num(max(vals)), _fmt_num(sum(vals) / len(vals)),
            ])
        body = _aligned(["SERIES", "LAST", "MIN", "MAX", "AVG"], rows)
    else:
        return f"(unsupported result type {rtype!r})"
    if truncated:
        body += f"\n… ({_MAX_SERIES_RENDERED}+ series, showing first " \
                f"{_MAX_SERIES_RENDERED})"
    return body


def _render_loki(streams: list[dict[str, Any]], limit: int) -> str:
    # Flatten all stream entries, tag with their stream labels, sort newest
    # first, and keep the most recent ``limit`` lines.
    entries: list[tuple[int, str, str]] = []
    for stream in streams:
        labels = stream.get("stream") or {}
        tag = ",".join(
            f"{k}={v}" for k, v in labels.items()
            if k in ("namespace", "app", "pod", "container", "job")
        )
        for ts, msg in stream.get("values", []):
            try:
                ts_ns = int(ts)
            except (ValueError, TypeError):
                ts_ns = 0
            entries.append((ts_ns, tag, msg))
    if not entries:
        return "(no log lines matched)"
    entries.sort(key=lambda e: e[0], reverse=True)
    lines = []
    for ts_ns, tag, msg in entries[:limit]:
        when = (datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
                .strftime("%H:%M:%S") if ts_ns else "--:--:--")
        lines.append(f"{when}  [{tag}]  {msg}")
    return "\n".join(lines)


def _is_num(v: Any) -> bool:
    try:
        float(v)
        return True
    except (ValueError, TypeError):
        return False


def _escape_logql(s: str) -> str:
    """Escape a substring so it's a safe literal inside a LogQL ``|~ "..."``
    double-quoted string (which the panel already validated for newlines)."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _humanise(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        body = (e.response.text or "")[:200]
        return f"HTTP {e.response.status_code}: {body}".strip()
    if isinstance(e, httpx.RequestError):
        return f"request failed: {e}"
    return f"{type(e).__name__}: {e}"


# ── OpenSearch (logs family, Lucene instead of LogQL) ──────────────────────

_OS_BY_ID = {p.id: p for p in OPENSEARCH_PANELS}
# Indices the console searches — fluent-bit writes kubernetes-YYYY.MM.DD.
_OS_INDEX_PATTERN = "kubernetes-*"


async def _os_request(
    base: str, auth: str | None, proxy: str | None, method: str, path: str,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_S, proxy=proxy, headers=_headers(auth)
    ) as client:
        r = await client.request(method, f"{base}{path}", json=json_body)
        r.raise_for_status()
        return r.json()


async def opensearch_overview(
    tenant_id: uuid.UUID | None, cluster: str | None = None
) -> LogsOverview:
    """Cluster health + index inventory, mapped onto the logs-overview shape:
    ``labels`` carries the (non-system) index names, ``namespaces`` the k8s
    namespaces present in the fluent-bit indices."""
    try:
        base, auth, proxy = await _resolve("opensearch", tenant_id, cluster)
    except ObsConsoleError as e:
        return LogsOverview(reachable=False, error=str(e))
    ov = LogsOverview(reachable=True, base_url=base)
    try:
        indices = await _os_request(
            base, auth, proxy, "GET", "/_cat/indices?format=json&h=index"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("obs.opensearch_overview_unreachable", error=str(e))
        return LogsOverview(reachable=False, base_url=base, error=_humanise(e))
    ov.labels = sorted(
        i["index"] for i in indices
        if isinstance(i, dict) and not str(i.get("index", "")).startswith(".")
    )
    ov.label_count = len(ov.labels)
    try:
        aggs = await _os_request(
            base, auth, proxy, "POST", f"/{_OS_INDEX_PATTERN}/_search",
            {"size": 0, "aggs": {"ns": {"terms": {
                "field": "kubernetes.namespace_name.keyword", "size": 100}}}},
        )
        ov.namespaces = sorted(
            b["key"] for b in
            aggs.get("aggregations", {}).get("ns", {}).get("buckets", [])
        )
    except Exception:  # noqa: BLE001 — namespaces are a nice-to-have
        pass
    return ov


def _render_os_hits(hits: list[dict[str, Any]], limit: int) -> str:
    if not hits:
        return "(no documents)"
    lines: list[str] = []
    for h in hits[:limit]:
        src = h.get("_source", {})
        kube = src.get("kubernetes") or {}
        where = "/".join(
            x for x in (kube.get("namespace_name"), kube.get("pod_name")) if x
        )
        lines.append(
            f"[{where}] {src.get('@timestamp', '')} "
            f"{(src.get('log') or src.get('message') or '')}".rstrip()
        )
    return "\n".join(lines)


async def _run_opensearch_queries(
    tenant_id: uuid.UUID | None,
    *,
    panel_ids: list[str],
    namespace: str | None,
    search: str | None,
    since: str,
    limit: int,
    actor_id: uuid.UUID | None,
    cluster: str | None = None,
) -> list[QueryResult]:
    namespace = (namespace or "").strip()
    if namespace and not _NS_RE.match(namespace):
        raise ObsConsoleError("invalid namespace filter")
    search = (search or "").strip()
    if search and not _SEARCH_RE.match(search):
        raise ObsConsoleError("invalid search filter")
    if not panel_ids:
        raise ObsConsoleError("select at least one panel")
    base, auth, proxy = await _resolve("opensearch", tenant_id, cluster)
    logger.info("obs.run_opensearch", tenant_id=str(tenant_id) if tenant_id
                else None, actor_id=str(actor_id) if actor_id else None,
                panels=panel_ids, namespace=namespace or None, since=since)

    results: list[QueryResult] = []
    for pid in panel_ids:
        panel = _OS_BY_ID.get(pid)
        if panel is None:
            raise ObsConsoleError(f"unknown panel {pid!r}")
        must: list[dict[str, Any]] = []
        filt: list[dict[str, Any]] = [
            # `since` values (5m/15m/1h/6h/24h) are valid ES date math as-is.
            {"range": {"@timestamp": {"gte": f"now-{since}"}}}
        ]
        if panel.line_filter:
            must.append({"query_string": {
                "query": panel.line_filter, "default_field": "log"}})
        if search:
            must.append({"query_string": {
                "query": f'"{search}"', "default_field": "log"}})
        if namespace:
            filt.append({"term": {
                "kubernetes.namespace_name.keyword": namespace}})
        body = {
            "size": limit,
            "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
            "query": {"bool": {"must": must, "filter": filt}},
        }
        display = panel.line_filter or "*"
        try:
            data = await _os_request(
                base, auth, proxy, "POST",
                f"/{_OS_INDEX_PATTERN}/_search", body,
            )
            hits = data.get("hits", {}).get("hits", [])
            results.append(QueryResult(
                id=pid, query=display, ok=True,
                output=_render_os_hits(hits, limit)))
        except Exception as e:  # noqa: BLE001
            results.append(QueryResult(
                id=pid, query=display, ok=False, output="",
                error=_humanise(e)))
    return results
