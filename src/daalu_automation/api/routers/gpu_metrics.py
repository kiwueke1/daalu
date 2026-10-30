"""AI Factory — native GPU observability + diagnostics for the hub UI.

Renders GPU metrics inside Daalu (no Grafana) by querying
kube-prometheus-stack's Prometheus, **always tenant-scoped server-side** so a
tenant only ever sees its own series. Surfaces doc 02 §3 (metrics/alerts), §4
(dcgmi diag / nccl), and §4A (observability validation) in the product UI.

Role-aware (see ``core/gpu/roles``):
* owner / provider — see their card's hardware metrics + run diagnostics.
* consumer         — see usage-centric metrics for their use of a shared card,
                     never another tenant's hardware health.
"""

from __future__ import annotations

import mimetypes
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import (
    current_admin,
    current_tenant_id,
    current_user,
)
from daalu_automation.config import get_settings
from daalu_automation.core import object_storage
from daalu_automation.core.gpu.roles import FactoryView, resolve_factory_view
from daalu_automation.core.prometheus import (
    PrometheusClient,
    PrometheusUnavailable,
    cached_query,
    client_for_tenant,
    escape_label_value,
    tenant_selector,
)
from daalu_automation.database import get_db
from daalu_automation.gpu_controller import aiperf as aiperf_build
from daalu_automation.gpu_controller.diagnostics import diag_stress_warning
from daalu_automation.models.aiperf_run import AiperfRun, AiperfRunState
from daalu_automation.models.daalu_hosted_quota import DaaluHostedQuota
from daalu_automation.models.gpu_diagnostic_run import (
    GpuDiagnosticKind,
    GpuDiagnosticRun,
    GpuDiagnosticState,
)
from daalu_automation.models.gpu_pool import GpuPool
from daalu_automation.models.gpu_tenant import GpuTenant
from daalu_automation.models.tenant import Tenant
from daalu_automation.models.user import User

# The inference-gateway in-cluster Service (the front door) — the alternate
# AIPerf target so an admin can isolate gateway overhead vs raw vLLM.
_GATEWAY_URL = "http://inference-gateway.daalu-automation.svc.cluster.local"

# The vLLM Service name the gpu-controller stamps per owner namespace
# (gpu_controller.manifests.SERVICE_NAME). An owner benchmarks their own card's
# endpoint at this Service in their namespace.
_OWNER_SERVICE = "llm-classifier"

router = APIRouter(prefix="/ai-factory", tags=["ai-factory"])

_RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}
_TS_METRICS = {
    "util": "DCGM_FI_DEV_GPU_UTIL",
    "temp": "DCGM_FI_DEV_GPU_TEMP",
    "power": "DCGM_FI_DEV_POWER_USAGE",
    "mem": "(DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE)) * 100",
}


# ── overview ───────────────────────────────────────────────────────────────


@router.get("/overview")
async def overview(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    view = await resolve_factory_view(db, tenant_id)
    # Must resolve the *same* Prometheus the metric endpoints will query — a
    # tenant whose store is its own tunnel-bound one would otherwise be told
    # metrics are unavailable (hub URL unset) or available (hub URL set) based
    # on an endpoint nothing actually reads.
    client = await client_for_tenant(db, tenant_id)
    return {
        "role": view.role,
        "has_gpu": view.has_gpu,
        "gpu_class": view.gpu_class,
        "metrics_available": client.configured and view.role != "none",
        "panels": view.panels,
    }


# ── metrics ──────────────────────────────────────────────────────────────


@router.get("/gpu/summary")
async def gpu_summary(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    view = await resolve_factory_view(db, tenant_id)
    if view.is_consumer and not view.has_gpu:
        return {"consumer": await _consumer_summary(db, tenant_id)}
    if not view.has_gpu:
        return {"gpus": [], "updated_at": _now_iso()}

    client = await client_for_tenant(db, tenant_id)
    if not client.configured:
        return {"gpus": [], "updated_at": _now_iso(), "metrics_available": False}
    sel = tenant_selector(view.tenant_label or str(tenant_id))

    by_gpu: dict[str, dict] = {}

    async def fold(metric: str, field: str, scale: float = 1.0):
        try:
            rows = await cached_query(client, f"{metric}{sel}")
        except PrometheusUnavailable:
            return
        for s in rows:
            # Key by a GLOBALLY-unique card identity, not the DCGM ``gpu``
            # index — every card is ``gpu="0"`` on a single-GPU node, so a
            # tenant with two sovereign cards (e.g. gpu-node-1 + gpu-node-3 over the
            # tunnel) would otherwise collapse into one row. The UUID is the
            # physical card's stable id; fall back to host:index if a series
            # lacks it.
            uuid_ = s.metric.get("UUID", "")
            hostname = s.metric.get("Hostname", s.metric.get("instance", ""))
            gpu_idx = s.metric.get("gpu", "0")
            key = uuid_ or f"{hostname}:{gpu_idx}"
            g = by_gpu.setdefault(
                key,
                {
                    # ``id`` is the stable selection key the UI deep-links on
                    # (detail view, per-card timeseries/events/alerts). ``gpu``
                    # stays for display (the on-node index).
                    "id": key,
                    "gpu": gpu_idx,
                    "uuid": uuid_,
                    "model": s.metric.get("modelName", ""),
                    "hostname": hostname,
                    "gpu_class": s.metric.get("gpu_class", view.gpu_class or ""),
                },
            )
            g[field] = round(s.value * scale, 2)

    await fold("DCGM_FI_DEV_GPU_TEMP", "temp_c")
    await fold("DCGM_FI_DEV_GPU_UTIL", "util_pct")
    await fold("DCGM_FI_DEV_FB_USED", "_mem_used_mib")
    await fold("DCGM_FI_DEV_FB_FREE", "_mem_free_mib")
    await fold("DCGM_FI_DEV_POWER_USAGE", "power_w")
    await fold("DCGM_FI_PROF_SM_ACTIVE", "sm_active_pct", scale=100.0)
    await fold("DCGM_FI_DEV_XID_ERRORS", "xid_errors")
    await fold("DCGM_FI_DEV_ECC_DBE_VOL_TOTAL", "ecc_dbe")

    gpus = []
    for g in by_gpu.values():
        used = g.pop("_mem_used_mib", 0.0)
        free = g.pop("_mem_free_mib", 0.0)
        total = used + free
        g["mem_used_gb"] = round(used / 1024, 2)
        g["mem_total_gb"] = round(total / 1024, 2)
        g["mem_pct"] = round((used / total) * 100, 1) if total else 0.0
        g.setdefault("util_pct", 0.0)
        g.setdefault("temp_c", 0.0)
        g.setdefault("power_w", 0.0)
        g.setdefault("sm_active_pct", 0.0)
        g["xid_errors"] = int(g.get("xid_errors", 0))
        g["ecc_dbe"] = int(g.get("ecc_dbe", 0))
        g["health"] = _health(g)
        gpus.append(g)

    return {"gpus": gpus, "updated_at": _now_iso()}


def _health(g: dict) -> str:
    if g.get("ecc_dbe", 0) > 0 or g.get("xid_errors", 0) > 0 or g.get("temp_c", 0) >= 90:
        return "crit"
    if g.get("temp_c", 0) >= 85 or g.get("mem_pct", 0) >= 97:
        return "warn"
    return "ok"


def _card_extra(card: str | None, gpu: str | None) -> dict[str, str] | None:
    """Label matcher scoping a query to a single physical card.

    The detail view passes the card's stable ``id`` (its DCGM ``UUID``) as
    ``?card=``; we scope on the UUID label so two cards that share a ``gpu``
    index (both ``gpu="0"`` on single-GPU nodes) stay distinct. Falls back to
    the legacy ``?gpu=<index>`` matcher when no card id is given.
    """
    if card:
        if card.startswith("GPU-"):
            return {"UUID": card}
        # Fallback id form "host:index" — scope by the index component.
        return {"gpu": card.rsplit(":", 1)[-1]}
    if gpu is not None:
        return {"gpu": gpu}
    return None


@router.get("/gpu/timeseries")
async def gpu_timeseries(
    metric: str = "util",
    range: str = "24h",  # noqa: A002 — matches the UI query param
    gpu: str | None = None,
    card: str | None = None,
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    if metric not in _TS_METRICS:
        raise HTTPException(400, f"unknown metric: {metric}")
    window = _RANGES.get(range, 86400)
    view = await resolve_factory_view(db, tenant_id)
    if not view.has_gpu:
        return {"metric": metric, "series": []}
    client = await client_for_tenant(db, tenant_id)
    if not client.configured:
        return {"metric": metric, "series": []}

    # Optional ?card=<uuid> (or legacy ?gpu=<index>) scopes the chart to one
    # card in the detail view.
    sel = tenant_selector(view.tenant_label or str(tenant_id), _card_extra(card, gpu))
    # Build an avg-over-gpus PromQL with the tenant selector injected on each
    # DCGM metric reference.
    promql = "avg(" + _inject_selector(_TS_METRICS[metric], sel) + ")"
    end = time.time()
    start = end - window
    step = max(15, window // 120)
    try:
        points = await client.query_range(promql, start=start, end=end, step_s=step)
    except PrometheusUnavailable:
        points = []
    return {
        "metric": metric,
        "series": [{"ts": int(ts), "value": round(v, 2)} for ts, v in points],
    }


def _inject_selector(expr: str, sel: str) -> str:
    """Append the tenant selector to each bare DCGM metric name in ``expr``."""
    import re

    return re.sub(r"(DCGM_FI_[A-Z0-9_]+)", lambda m: m.group(1) + sel, expr)


@router.get("/gpu/events")
async def gpu_events(
    gpu: str | None = None,
    card: str | None = None,
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    view = await resolve_factory_view(db, tenant_id)
    if not view.has_gpu:
        return {"events": []}
    client = await client_for_tenant(db, tenant_id)
    if not client.configured:
        return {"events": []}
    sel = tenant_selector(view.tenant_label or str(tenant_id), _card_extra(card, gpu))
    events = []
    checks = [
        ("DCGM_FI_DEV_XID_ERRORS", "xid", "XID error counter"),
        ("DCGM_FI_DEV_ECC_DBE_VOL_TOTAL", "ecc_dbe", "uncorrectable ECC (DBE)"),
        ("DCGM_FI_DEV_ECC_SBE_VOL_TOTAL", "ecc_sbe", "corrected ECC (SBE)"),
    ]
    for metric, kind, label in checks:
        try:
            rows = await cached_query(client, f"{metric}{sel}")
        except PrometheusUnavailable:
            continue
        for s in rows:
            if s.value > 0:
                events.append(
                    {
                        "ts": _now_iso(),
                        "gpu": s.metric.get("gpu", "0"),
                        "kind": kind,
                        "detail": f"{label}: {int(s.value)} on {s.metric.get('Hostname', '')}",
                    }
                )
    return {"events": events}


# Alertmanager meta-alerts that inherit GPU labels (so they match
# component="gpu") but aren't real hardware alerts — never show these.
_META_ALERTS = frozenset({"InfoInhibitor", "Watchdog"})


@router.get("/alerts")
async def gpu_alerts(
    gpu: str | None = None,
    card: str | None = None,
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    view = await resolve_factory_view(db, tenant_id)
    client = await client_for_tenant(db, tenant_id)
    if not client.configured or not view.has_gpu:
        return {"alerts": []}
    want_tenant = view.tenant_label or str(tenant_id)
    # Detail view scopes to one card: prefer the globally-unique UUID (two
    # single-GPU cards both report gpu="0"); fall back to the gpu index.
    want_uuid = card if (card and card.startswith("GPU-")) else None
    want_gpu = card.rsplit(":", 1)[-1] if (card and not want_uuid) else gpu
    # GPU alert rules carry component="gpu"; tenant-scope where the series has it.
    promql = 'ALERTS{component="gpu",alertstate=~"firing|pending"}'
    try:
        rows = await cached_query(client, promql)
    except PrometheusUnavailable:
        return {"alerts": []}
    alerts = []
    for s in rows:
        name = s.metric.get("alertname", "alert")
        if name in _META_ALERTS:
            continue
        # Only show alerts whose series is for this tenant when a tenant label
        # is present; alerts without a tenant label are operator-wide and shown
        # to the provider/owner.
        a_tenant = s.metric.get("tenant")
        if a_tenant and a_tenant != want_tenant:
            continue
        a_gpu = s.metric.get("gpu")
        a_uuid = s.metric.get("UUID")
        # Scope to one card when the detail view asks: match the UUID if the
        # alert carries one, else the gpu index. The overview passes neither
        # and shows every card's alerts.
        if want_uuid is not None and a_uuid is not None:
            if a_uuid != want_uuid:
                continue
        elif want_gpu is not None and a_gpu is not None and a_gpu != want_gpu:
            continue
        alerts.append(
            {
                "name": name,
                "gpu": a_gpu,
                "uuid": a_uuid,
                "severity": s.metric.get("severity", "warning"),
                "state": s.metric.get("alertstate", "firing"),
                "summary": s.metric.get("alertname", ""),
                "since": _now_iso(),
            }
        )
    return {"alerts": alerts}


# ── consumer view ────────────────────────────────────────────────────────


async def _consumer_summary(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    from daalu_automation.core.billing import current_period_total

    totals = await current_period_total(db, tenant_id)
    quota = (
        await db.execute(
            select(DaaluHostedQuota).where(DaaluHostedQuota.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    avg_latency = (
        await db.execute(
            select(func.coalesce(func.avg(_usage_latency()), 0)).where(
                _usage_tenant() == tenant_id
            )
        )
    ).scalar() or 0
    return {
        "tokens_prompt": totals.prompt_tokens,
        "tokens_completion": totals.completion_tokens,
        "requests": totals.events,
        "quota_used": quota.current_period_used if quota else 0,
        "quota_limit": quota.monthly_token_limit if quota else 0,
        "avg_latency_ms": int(avg_latency),
        # We deliberately do NOT expose the provider card's raw utilisation.
        "pool_util_pct": None,
    }


def _usage_latency():
    from daalu_automation.models.billing import UsageEvent

    return UsageEvent.latency_ms


def _usage_tenant():
    from daalu_automation.models.billing import UsageEvent

    return UsageEvent.tenant_id


# ── observability validation (doc 02 §4A, read-only) ─────────────────────


@router.post("/observability/validate")
async def validate_observability(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
    user: User = Depends(current_admin),
):
    view = await resolve_factory_view(db, tenant_id)
    client = await client_for_tenant(db, tenant_id)
    checks: list[dict] = []

    def add(name: str, status: str, detail: str):
        checks.append({"name": name, "status": status, "detail": detail})

    if not client.configured:
        add("prometheus_configured", "fail", "prometheus_base_url is not set")
        passed = False
    else:
        add("prometheus_configured", "pass", client.base_url)
        # dcgm-exporter target up (any DCGM-emitting target, however wired)
        try:
            up = await client.dcgm_target_up()
            add(
                "dcgm_target_up",
                "pass" if up else "fail",
                "a DCGM exporter target is up" if up else "no DCGM exporter target is up",
            )
        except PrometheusUnavailable as e:
            add("dcgm_target_up", "fail", str(e))
        # DCGM series present
        n = await _safe_scalar(client, "count(DCGM_FI_DEV_GPU_TEMP)")
        add(
            "dcgm_series_present",
            "pass" if (n or 0) > 0 else "fail",
            f"{int(n or 0)} GPU temp series",
        )
        # per-tenant labels (only if this tenant owns/provides a card)
        if view.has_gpu:
            sel = tenant_selector(view.tenant_label or str(tenant_id))
            nt = await _safe_scalar(client, f"count(DCGM_FI_DEV_GPU_TEMP{sel})")
            add(
                "per_tenant_labels",
                "pass" if (nt or 0) > 0 else "fail",
                f"{int(nt or 0)} series carry this tenant label",
            )
        else:
            add("per_tenant_labels", "skip", "no card owned by this tenant")
        # alert pipeline alive (Watchdog)
        w = await _safe_scalar(client, 'count(ALERTS{alertname="Watchdog"})')
        add(
            "alert_pipeline",
            "pass" if (w or 0) > 0 else "skip",
            "Watchdog alert present" if (w or 0) > 0 else "Watchdog not found",
        )
        passed = all(c["status"] != "fail" for c in checks)

    now = datetime.now(tz=timezone.utc)
    run = GpuDiagnosticRun(
        tenant_id=tenant_id,
        kind=GpuDiagnosticKind.observability_validate,
        state=GpuDiagnosticState.passed if passed else GpuDiagnosticState.failed,
        summary={"checks": checks, "passed": passed},
        requested_by=user.email,
        started_at=now,
        finished_at=now,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return {"run_id": str(run.id), "checks": checks, "passed": passed}


async def _safe_scalar(client: PrometheusClient, promql: str) -> float | None:
    try:
        return await client.query_scalar(promql, default=0.0)
    except PrometheusUnavailable:
        return None


# ── diagnostics (dcgmi diag / nccl) ──────────────────────────────────────


class DiagnosticRequest(BaseModel):
    kind: str  # "dcgmi_diag" | "nccl_test"
    level: int | None = None
    # Stressful runs (dcgmi -r2/-r3, NCCL) require the caller to confirm they
    # understand the GPU load — the UI shows the warning and re-submits with
    # acknowledged=true.
    acknowledged: bool = False


@router.post("/gpu/diagnostics")
async def run_diagnostic(
    req: DiagnosticRequest,
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
    user: User = Depends(current_admin),
):
    view = await resolve_factory_view(db, tenant_id)
    if not view.has_gpu:
        raise HTTPException(403, "no GPU owned/provided by this tenant")
    try:
        kind = GpuDiagnosticKind(req.kind)
    except ValueError:
        raise HTTPException(400, f"unknown diagnostic kind: {req.kind}")
    if kind == GpuDiagnosticKind.observability_validate:
        raise HTTPException(400, "use POST /ai-factory/observability/validate")

    # Gate stressful runs behind an explicit acknowledgement.
    warning = diag_stress_warning(req.kind, req.level)
    if warning and not req.acknowledged:
        raise HTTPException(
            status_code=412,
            detail={
                "requires_acknowledgement": True,
                "warning": warning,
                "kind": req.kind,
                "level": req.level,
            },
        )

    gpu = (
        await db.execute(select(GpuTenant).where(GpuTenant.tenant_id == tenant_id))
    ).scalar_one_or_none()
    run = GpuDiagnosticRun(
        tenant_id=tenant_id,
        gpu_tenant_id=gpu.id if gpu else None,
        kind=kind,
        level=req.level if kind == GpuDiagnosticKind.dcgmi_diag else None,
        state=GpuDiagnosticState.pending,
        requested_by=user.email,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return {"id": str(run.id), "state": run.state.value}


@router.get("/gpu/diagnostics")
async def list_diagnostics(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    rows = (
        (
            await db.execute(
                select(GpuDiagnosticRun)
                .where(GpuDiagnosticRun.tenant_id == tenant_id)
                .order_by(GpuDiagnosticRun.created_at.desc())
                .limit(25)
            )
        )
        .scalars()
        .all()
    )
    return {"runs": [_run_view(r, with_output=False) for r in rows]}


@router.get("/gpu/diagnostics/{run_id}")
async def get_diagnostic(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    run = await db.get(GpuDiagnosticRun, run_id)
    if run is None or run.tenant_id != tenant_id:
        raise HTTPException(404, "diagnostic run not found")
    return _run_view(run, with_output=True)


def _run_view(r: GpuDiagnosticRun, *, with_output: bool) -> dict:
    out = {
        "id": str(r.id),
        "kind": r.kind.value,
        "level": r.level,
        "state": r.state.value,
        "summary": r.summary or None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }
    if with_output:
        out["output"] = r.output
    return out


# ── AIPerf — load-test / SLO benchmarking (site operators + GPU owners) ──────
#
# AIPerf is a load generator: a full sweep IS load on the endpoint under test.
# Access is therefore restricted (``current_aiperf_access``):
#  * site superuser — benchmarks the operator's shared serving stack; free
#    choice of target (raw vLLM / gateway / explicit URL); sees every run.
#  * GPU owner / provider (tenant admin with a card) — benchmarks ONLY their
#    own endpoint (their namespace's vLLM Service, or their pool's upstream);
#    an arbitrary target_url is rejected; sees only their own runs.
#  * everyone else (consumers, non-admins) — 403.
# The exec itself is still gated by ``gpu_aiperf_exec_enabled`` in the
# controller; when off, a queued run is failed fast with a clear message.


@dataclass
class AiperfAccess:
    user: User
    view: FactoryView
    site: bool  # superuser → site-wide scope + free target choice


async def current_aiperf_access(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> AiperfAccess:
    """Allow a site superuser, or a tenant admin who owns/provides a GPU."""
    if user.is_superuser:
        view = await resolve_factory_view(db, user.tenant_id)
        return AiperfAccess(user=user, view=view, site=True)
    if not user.is_admin:
        raise HTTPException(403, "admin privileges required")
    view = await resolve_factory_view(db, user.tenant_id)
    if not view.has_gpu:
        raise HTTPException(
            403,
            "AIPerf is available to site operators and GPU owners/providers only",
        )
    return AiperfAccess(user=user, view=view, site=False)


class AiperfRunRequest(BaseModel):
    model: str = aiperf_build.DEFAULT_MODEL
    # Sweep + load shape. Defaults match scripts/aiperf-bench.sh.
    concurrency: str = aiperf_build.DEFAULT_CONCURRENCY
    request_count: int = 200
    input_tokens: int = 512
    output_tokens: int = 256
    endpoint_type: str = "chat"
    streaming: bool = True
    # Benchmark the front door (gateway) instead of raw vLLM. When true and no
    # explicit target_url is given, the gateway Service URL is used. Both are
    # honoured for a site superuser only — an owner/provider always targets
    # their own endpoint.
    via_gateway: bool = False
    target_url: str | None = None


async def _resolve_aiperf_target(
    db: AsyncSession, access: AiperfAccess, req: AiperfRunRequest
) -> tuple[str, str, bool]:
    """Resolve (target_url, model, via_gateway) honouring the caller's scope.

    A site superuser picks freely; an owner/provider is pinned to their own
    endpoint and may not pass an arbitrary ``target_url``.
    """
    if access.site:
        target = req.target_url or (
            _GATEWAY_URL if req.via_gateway else aiperf_build.DEFAULT_TARGET_URL
        )
        return target, req.model, req.via_gateway

    if req.target_url:
        raise HTTPException(403, "you may only benchmark your own endpoint")

    tenant_id = access.user.tenant_id
    if access.view.is_provider:
        pool = (
            await db.execute(
                select(GpuPool).where(GpuPool.provider_tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if pool is None or not pool.upstream_url:
            raise HTTPException(409, "no serving endpoint found for your GPU pool")
        served = list(pool.served_models or [])
        model = req.model if req.model in served else (served[0] if served else req.model)
        return pool.upstream_url, model, False

    # owner — their own card's vLLM Service in their namespace (or a configured
    # SOVEREIGN endpoint).
    gpu = (
        await db.execute(select(GpuTenant).where(GpuTenant.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if gpu is None:
        raise HTTPException(409, "no serving endpoint found for your GPU")
    tenant = await db.get(Tenant, tenant_id)
    if tenant is not None and tenant.sovereign_inference_url:
        target = tenant.sovereign_inference_url
    else:
        target = f"http://{_OWNER_SERVICE}.{gpu.namespace}.svc.cluster.local:80"
    return target, (gpu.model_classifier or req.model), False


@router.post("/aiperf/runs")
async def run_aiperf(
    req: AiperfRunRequest,
    db: AsyncSession = Depends(get_db),
    access: AiperfAccess = Depends(current_aiperf_access),
):
    target, model, via_gateway = await _resolve_aiperf_target(db, access, req)
    run = AiperfRun(
        # The run's owning tenant — for a superuser this is provenance (the run
        # is site-wide); for an owner/provider it is the scope their list/detail
        # queries filter on.
        tenant_id=access.user.tenant_id,
        state=AiperfRunState.pending,
        target_url=target,
        model=model,
        endpoint_type=req.endpoint_type,
        concurrency=aiperf_build.normalise_concurrency(req.concurrency),
        request_count=max(1, min(int(req.request_count), 2000)),
        input_tokens=max(1, min(int(req.input_tokens), 8192)),
        output_tokens=max(1, min(int(req.output_tokens), 8192)),
        streaming=req.streaming,
        via_gateway=via_gateway,
        requested_by=access.user.email,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return {"id": str(run.id), "state": run.state.value}


@router.get("/aiperf/runs")
async def list_aiperf_runs(
    db: AsyncSession = Depends(get_db),
    access: AiperfAccess = Depends(current_aiperf_access),
):
    q = select(AiperfRun).order_by(AiperfRun.created_at.desc()).limit(25)
    # Site superuser sees every run; an owner/provider sees only their own.
    if not access.site:
        q = q.where(AiperfRun.tenant_id == access.user.tenant_id)
    rows = (await db.execute(q)).scalars().all()
    return {
        "runs": [_aiperf_view(r, with_output=False) for r in rows],
        "exec_enabled": get_settings().gpu_aiperf_exec_enabled,
        "scope": "site" if access.site else access.view.role,
    }


async def _aiperf_run_or_404(
    db: AsyncSession, run_id: uuid.UUID, access: AiperfAccess
) -> AiperfRun:
    run = await db.get(AiperfRun, run_id)
    # An owner/provider can only see their own runs; 404 (not 403) so we don't
    # leak the existence of another tenant's run.
    if run is None or (not access.site and run.tenant_id != access.user.tenant_id):
        raise HTTPException(404, "aiperf run not found")
    return run


@router.get("/aiperf/runs/{run_id}")
async def get_aiperf_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    access: AiperfAccess = Depends(current_aiperf_access),
):
    run = await _aiperf_run_or_404(db, run_id, access)
    return _aiperf_view(run, with_output=True)


@router.get("/aiperf/runs/{run_id}/artifacts")
async def list_aiperf_artifacts(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    access: AiperfAccess = Depends(current_aiperf_access),
):
    """The downloadable artifact manifest the uploader pushed to object storage
    (``profile_export_aiperf.csv/json`` + logs, per ISL/OSL/concurrency)."""
    run = await _aiperf_run_or_404(db, run_id, access)
    summary = run.summary or {}
    return {
        "run_id": str(run.id),
        "artifacts": summary.get("artifacts", []),
        "artifacts_error": summary.get("artifacts_error"),
    }


@router.get("/aiperf/runs/{run_id}/artifacts/{path:path}")
async def download_aiperf_artifact(
    run_id: uuid.UUID,
    path: str,
    db: AsyncSession = Depends(get_db),
    access: AiperfAccess = Depends(current_aiperf_access),
):
    """Stream a single artifact file from object storage."""
    run = await _aiperf_run_or_404(db, run_id, access)
    summary = run.summary or {}
    known = {a.get("path") for a in summary.get("artifacts", [])}
    # Only serve files in this run's recorded manifest — bounds the request to
    # the run's own prefix and blocks arbitrary-key reads / path traversal.
    if path not in known:
        raise HTTPException(404, "artifact not found")
    bucket = summary.get("artifacts_bucket") or get_settings().s3_bucket_aiperf
    key = f"aiperf/{run.id}/{path}"
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    filename = path.rsplit("/", 1)[-1]
    return StreamingResponse(
        # AIPerf artifacts live in the workload store, reached over the tunnel.
        object_storage.iter_object(bucket, key, client=object_storage.aiperf_client()),
        media_type=ctype,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _aiperf_view(r: AiperfRun, *, with_output: bool) -> dict:
    out = {
        "id": str(r.id),
        "state": r.state.value,
        "model": r.model,
        "target_url": r.target_url,
        "via_gateway": r.via_gateway,
        "endpoint_type": r.endpoint_type,
        "concurrency": r.concurrency,
        "request_count": r.request_count,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "summary": r.summary or None,
        "requested_by": r.requested_by,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }
    if with_output:
        out["output"] = r.output
    return out


# ── Reliability — NVSentinel auto-remediation + cuda-checkpoint (read-only) ─
#
# Surfaces doc 03 in the product UI. Read-only and role-scoped: an owner/provider
# sees the reliability posture of THEIR card — DCGM health signals (XID/ECC/
# thermal), whether NVSentinel auto-remediation is active (it watches the same
# DCGM stream and cordons/reboots a faulted node), and the cuda-checkpoint
# status (gated behind legal sign-off). The hub never drives remediation — it
# only reads NVSentinel's exported metrics.


@router.get("/reliability")
async def reliability(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    view = await resolve_factory_view(db, tenant_id)
    settings = get_settings()
    cuda_checkpoint = {
        "enabled": settings.gpu_cuda_checkpoint_enabled,
        # Proprietary EULA — never "available" in the product until legal clears
        # it (doc 03 §2.1). The flag default is False.
        "status": "enabled"
        if settings.gpu_cuda_checkpoint_enabled
        else "gated",
        "note": (
            "CUDA checkpoint/restore is proprietary NVIDIA software (EULA) and "
            "requires legal sign-off before productisation."
        ),
    }
    if not view.has_gpu:
        return {
            "status": "n/a",
            "has_gpu": False,
            "signals": [],
            "nvsentinel": {"active": False},
            "cuda_checkpoint": cuda_checkpoint,
        }

    client = await client_for_tenant(db, tenant_id)
    if not client.configured:
        return {
            "status": "unknown",
            "has_gpu": True,
            "metrics_available": False,
            "signals": [],
            "nvsentinel": {"active": False},
            "cuda_checkpoint": cuda_checkpoint,
        }

    sel = tenant_selector(view.tenant_label or str(tenant_id))
    signals: list[dict] = []

    async def scalar(promql: str) -> float:
        return (await _safe_scalar(client, promql)) or 0.0

    xid = await scalar(f"max(DCGM_FI_DEV_XID_ERRORS{sel})")
    ecc_dbe = await scalar(f"max(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL{sel})")
    temp = await scalar(f"max(DCGM_FI_DEV_GPU_TEMP{sel})")

    signals.append(_signal("XID errors", xid, crit=xid > 0))
    signals.append(_signal("Uncorrectable ECC (DBE)", ecc_dbe, crit=ecc_dbe > 0))
    signals.append(
        _signal(
            "Max temperature",
            temp,
            crit=temp >= 90,
            warn=temp >= 85,
            unit="°C",
        )
    )

    if xid > 0 or ecc_dbe > 0 or temp >= 90:
        status = "crit"
    elif temp >= 85:
        status = "warn"
    else:
        status = "ok"

    # Is NVSentinel deployed + scraped? (Its ServiceMonitor exposes `up` under
    # the configured job.) If it's not active, the UI shows "auto-remediation
    # not active — faults page a human via the runbook".
    job = escape_label_value(settings.nvsentinel_metrics_job)
    ns_up = await scalar(f'max(up{{job="{job}"}})')
    # Best-effort remediation counter — NVSentinel exposes remediation actions;
    # the exact series name varies by version, so match broadly and tolerate a
    # miss (0). This drives a "N remediations" stat, not a gate.
    remediations = await scalar(
        f'sum(nvsentinel_remediations_total) or sum(up{{job="{job}"}}) * 0'
    )

    return {
        "status": status,
        "has_gpu": True,
        "metrics_available": True,
        "signals": signals,
        "nvsentinel": {
            "active": ns_up >= 1.0,
            "remediations": int(remediations),
            "mode": "observe",  # we ship NVSentinel in observe/dry-run first
        },
        "cuda_checkpoint": cuda_checkpoint,
        "updated_at": _now_iso(),
    }


def _signal(
    name: str,
    value: float,
    *,
    crit: bool = False,
    warn: bool = False,
    unit: str = "",
) -> dict:
    level = "crit" if crit else "warn" if warn else "ok"
    return {
        "name": name,
        "value": round(value, 1),
        "unit": unit,
        "level": level,
    }


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
