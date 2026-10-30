"""Thin async Prometheus client for the native AI-factory GPU UI.

The hub renders GPU metrics itself (no Grafana) by querying
**kube-prometheus-stack's Prometheus** — NOT Thanos, which has no stores
([[agent_observability_data_plane]]). This module is a small wrapper over the
Prometheus HTTP API (`/api/v1/query`, `/api/v1/query_range`) plus helpers that
build **tenant-scoped** DCGM selectors so a tenant can only ever read its own
series (the label injection happens server-side, never from the browser).

Kept deliberately minimal and dependency-free (httpx only) so it is trivially
mockable in tests via ``httpx.MockTransport``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from daalu_automation.config import get_settings

logger = structlog.get_logger(__name__)


class PrometheusUnavailable(Exception):
    """Prometheus is not configured or unreachable — UI shows 'unavailable'."""


def escape_label_value(value: str) -> str:
    r"""Escape a PromQL label value (backslash and double-quote)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def tenant_selector(tenant_id: str, extra: dict[str, str] | None = None) -> str:
    """Build a ``{tenant="<id>",k="v",...}`` matcher with the tenant pinned.

    The tenant label is always present and is the security boundary — callers
    pass the *authenticated* tenant id, never anything from the request body.
    """
    parts = [f'tenant="{escape_label_value(tenant_id)}"']
    for k, v in (extra or {}).items():
        parts.append(f'{k}="{escape_label_value(v)}"')
    return "{" + ",".join(parts) + "}"


@dataclass(slots=True)
class InstantSample:
    metric: dict[str, str]
    value: float
    ts: float


class PrometheusClient:
    """Async client over the Prometheus HTTP API. One per request is fine."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout_s: float | None = None,
        proxy: str | None = None,
    ):
        s = get_settings()
        self.base_url = (base_url if base_url is not None else s.prometheus_base_url).rstrip("/")
        self.timeout_s = timeout_s if timeout_s is not None else s.prometheus_query_timeout_s
        # daalu-edge HTTP forward proxy (``http://<tunnel_ip>:8888``) when the
        # Prometheus lives in a customer cluster and its URL only resolves
        # there (``*.svc.cluster.local``). See core/cluster_proxy.py.
        self.proxy = proxy

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def query(self, promql: str) -> list[InstantSample]:
        """Instant query → list of samples (one per series). [] on no data."""
        data = await self._get("/api/v1/query", {"query": promql})
        out: list[InstantSample] = []
        for r in data.get("result", []):
            val = r.get("value")  # [ <ts>, "<float>" ]
            if not val or len(val) != 2:
                continue
            try:
                out.append(
                    InstantSample(metric=r.get("metric", {}), value=float(val[1]), ts=float(val[0]))
                )
            except (TypeError, ValueError):
                continue
        return out

    async def query_scalar(self, promql: str, default: float | None = None) -> float | None:
        """First sample's value, or ``default`` when the query is empty."""
        rows = await self.query(promql)
        return rows[0].value if rows else default

    async def query_range(
        self, promql: str, *, start: float, end: float, step_s: int
    ) -> list[tuple[float, float]]:
        """Range query → [(ts, value)] for the first matching series."""
        data = await self._get(
            "/api/v1/query_range",
            {"query": promql, "start": start, "end": end, "step": step_s},
        )
        result = data.get("result", [])
        if not result:
            return []
        values = result[0].get("values", [])
        out: list[tuple[float, float]] = []
        for ts, v in values:
            try:
                out.append((float(ts), float(v)))
            except (TypeError, ValueError):
                continue
        return out

    async def target_up(self, job: str) -> bool:
        """Is at least one target of ``job`` currently up?"""
        v = await self.query_scalar(f'max(up{{job="{escape_label_value(job)}"}})', default=0.0)
        return bool(v and v >= 1.0)

    async def dcgm_target_up(self) -> bool:
        """Is at least one *DCGM-exporting* scrape target currently up?

        We do NOT hard-code the job name: in-cluster the GPU Operator scrapes a
        ``nvidia-dcgm`` job, but in prod the cards live on the workload cluster
        and their series are federated over the WireGuard tunnel under a
        per-tenant job (``gpu-tunnel-dcgm-<tenant>``). Both are correct. So we
        ask the question that actually matters — "is a target that emits DCGM
        GPU-temp series up?" — by intersecting ``up`` with the DCGM metric on
        ``(job, instance)``. This stays true regardless of how the exporter is
        wired up ([[agent_observability_data_plane]]).
        """
        v = await self.query_scalar(
            "max(up and on (job, instance) DCGM_FI_DEV_GPU_TEMP)", default=0.0
        )
        return bool(v and v >= 1.0)

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise PrometheusUnavailable("prometheus_base_url is not configured")
        url = self.base_url + path
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_s, proxy=self.proxy
            ) as client:
                r = await client.get(url, params=params)
        except httpx.HTTPError as e:
            logger.warning("prometheus.transport_error", error=str(e))
            raise PrometheusUnavailable(f"prometheus unreachable: {e}") from e
        if r.status_code != 200:
            logger.warning("prometheus.bad_status", status=r.status_code, body=r.text[:300])
            raise PrometheusUnavailable(f"prometheus returned {r.status_code}")
        body = r.json()
        if body.get("status") != "success":
            raise PrometheusUnavailable(f"prometheus error: {body.get('error', 'unknown')}")
        return body.get("data", {})


async def client_for_tenant(
    db: Any, tenant_id: Any, *, provider: str = "prometheus"
) -> PrometheusClient:
    """Prometheus for ``tenant_id``, preferring its own over the hub's.

    GPU metrics come from a ``dcgm-exporter`` running in whichever cluster the
    card is in. When that's a customer cluster reached over the tunnel, its
    series are in *that* cluster's Prometheus — the hub's own Prometheus has
    none of them, and the AI-factory page rendered "No GPUs are reporting
    metrics yet" for a GPU that was demonstrably serving.

    Resolution order:

    1. The tenant's ``prometheus`` Integration bound to a **connected**
       cluster tunnel → dialed through that edge's forward proxy, so
       ``*.svc.cluster.local`` URLs resolve inside the customer cluster.
    2. The same Integration with no tunnel (a directly-reachable URL).
    3. ``settings.prometheus_base_url`` — the hub's own store.

    Falling back rather than failing keeps single-cluster installs, where the
    hub Prometheus scrapes everything, working with no integration row at all.
    """
    from sqlalchemy import select as _select

    from daalu_automation.core.cluster_proxy import get_proxy_url
    from daalu_automation.models import Integration

    row = (
        await db.execute(
            _select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.provider == provider,
            )
        )
    ).scalar_one_or_none()

    url = ((row.config or {}).get("url") or "").strip() if row else ""
    if url:
        proxy = await get_proxy_url(db, row.cluster_tunnel_id)
        # A tunnel-bound row whose tunnel is down yields proxy=None; an
        # in-cluster URL would then resolve against the hub and silently
        # return nothing, so prefer the hub's own store explicitly.
        if row.cluster_tunnel_id is None or proxy is not None:
            return PrometheusClient(base_url=url, proxy=proxy)

    return PrometheusClient()


# ── tiny instant-query cache (per-process, short TTL) ──────────────────────
_CACHE: dict[str, tuple[float, list[InstantSample]]] = {}


async def cached_query(client: PrometheusClient, promql: str) -> list[InstantSample]:
    """Instant query with a short TTL cache, so UI polling is cheap.

    Keyed by (endpoint, promql), not promql alone: once a query can target a
    *tenant's* Prometheus rather than only the hub's, a bare-promql key would
    hand one tenant's series to another for the length of the TTL. The proxy
    is part of the identity because the same in-cluster URL means a different
    store behind a different tunnel.
    """
    ttl = get_settings().prometheus_cache_ttl_s
    now = time.monotonic()
    key = f"{client.proxy or '-'}|{client.base_url}|{promql}"
    hit = _CACHE.get(key)
    if hit is not None and (now - hit[0]) < ttl:
        return hit[1]
    rows = await client.query(promql)
    _CACHE[key] = (now, rows)
    return rows
