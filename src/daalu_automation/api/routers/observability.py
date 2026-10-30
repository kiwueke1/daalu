"""Observability console API — read-only metrics (Prometheus/Thanos) + logs (Loki).

The metrics/logs analogue of the kubectl console (:mod:`.clusters`). Each
connected ``Integration`` (provider ``prometheus`` / ``thanos`` / ``loki``)
gets a detail page under ``/observability/{provider}`` in the UI, driven by
:mod:`daalu_automation.core.observability_console`:

* ``GET  /observability/{provider}/overview`` — headline health/inventory.
* ``GET  /observability/{provider}/catalog``  — the allowlisted query panels.
* ``POST /observability/{provider}/query``     — run ticked panels (+ for
  metrics, one optional free-form PromQL) and return rendered output.

Everything is a single read against the store's HTTP query API; nothing here
mutates state. Reads use ``current_tenant_id``; running queries uses
``current_admin`` to match the kubectl runner.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_admin, current_tenant_id
from daalu_automation.core import observability_console as obs
from daalu_automation.database import get_db
from daalu_automation.models import User

router = APIRouter(prefix="/observability", tags=["observability"])


# ── schemas (mirror frontend/lib/api.ts) ─────────────────────────────────


class ObsQuerySpecOut(BaseModel):
    id: str
    label: str
    group: str
    query: str
    description: str
    unit: str = ""


class JobHealthOut(BaseModel):
    job: str
    up: int
    total: int


class MetricsOverviewOut(BaseModel):
    base_url: str
    version: str | None = None
    targets_total: int | None = None
    targets_up: int | None = None
    targets_down: int | None = None
    firing_alerts: int | None = None
    jobs: list[JobHealthOut] = Field(default_factory=list)


class LogsOverviewOut(BaseModel):
    base_url: str
    label_count: int | None = None
    labels: list[str] = Field(default_factory=list)
    namespaces: list[str] = Field(default_factory=list)


class ObservabilityOverviewOut(BaseModel):
    provider: str
    family: str  # "metrics" | "logs"
    reachable: bool
    error: str | None = None
    metrics: MetricsOverviewOut | None = None
    logs: LogsOverviewOut | None = None


class ObsQueryIn(BaseModel):
    query_ids: list[str] = Field(default_factory=list, max_length=25)
    # metrics
    custom_query: str | None = Field(default=None, max_length=2000)
    time_range: str = "instant"
    # logs
    namespace: str | None = Field(default=None, max_length=128)
    search: str | None = Field(default=None, max_length=200)
    since: str = "1h"
    limit: int = Field(default=200, ge=1, le=1000)


class ObsQueryResultOut(BaseModel):
    id: str
    query: str
    ok: bool
    output: str
    error: str | None = None


class ObsQueryRunOut(BaseModel):
    results: list[ObsQueryResultOut]


# ── helpers ──────────────────────────────────────────────────────────────


def _family_or_404(provider: str) -> str:
    family = obs.family_for(provider)
    if family is None:
        raise HTTPException(
            404,
            f"no observability console for provider {provider!r} "
            "(supported: prometheus, thanos, loki)",
        )
    return family


# ── endpoints ──────────────────────────────────────────────────────────────


@router.get("/{provider}/overview", response_model=ObservabilityOverviewOut)
async def overview(
    provider: str,
    cluster: str | None = Query(
        default=None, max_length=64,
        description="ClusterTunnel slug: use that cluster's store, not the tenant's.",
    ),
    _tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    family = _family_or_404(provider)
    if family == "metrics":
        ov = await obs.metrics_overview(provider, _tenant_id, cluster)
        return ObservabilityOverviewOut(
            provider=provider, family=family, reachable=ov.reachable,
            error=ov.error,
            metrics=MetricsOverviewOut(
                base_url=ov.base_url, version=ov.version,
                targets_total=ov.targets_total, targets_up=ov.targets_up,
                targets_down=ov.targets_down, firing_alerts=ov.firing_alerts,
                jobs=[JobHealthOut(job=j.job, up=j.up, total=j.total)
                      for j in ov.jobs],
            ),
        )
    lov = await obs.logs_overview(provider, _tenant_id, cluster)
    return ObservabilityOverviewOut(
        provider=provider, family=family, reachable=lov.reachable,
        error=lov.error,
        logs=LogsOverviewOut(
            base_url=lov.base_url, label_count=lov.label_count,
            labels=lov.labels, namespaces=lov.namespaces,
        ),
    )


@router.get("/{provider}/catalog", response_model=list[ObsQuerySpecOut])
async def catalog(
    provider: str,
    _tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    family = _family_or_404(provider)
    if family == "metrics":
        return [
            ObsQuerySpecOut(id=p.id, label=p.label, group=p.group,
                            query=p.promql, description=p.description,
                            unit=p.unit)
            for p in obs.metrics_catalog()
        ]
    return [
        ObsQuerySpecOut(id=p.id, label=p.label, group=p.group,
                        query=obs.log_display_query(p),
                        description=p.description)
        for p in obs.logs_catalog(provider)
    ]


@router.post("/{provider}/query", response_model=ObsQueryRunOut)
async def run_query(
    provider: str,
    body: ObsQueryIn,
    cluster: str | None = Query(default=None, max_length=64),
    user: User = Depends(current_admin),
):
    family = _family_or_404(provider)
    try:
        if family == "metrics":
            results = await obs.run_metric_queries(
                provider, user.tenant_id,
                panel_ids=body.query_ids, time_range=body.time_range,
                custom_query=body.custom_query, actor_id=user.id,
                cluster=cluster,
            )
        else:
            results = await obs.run_log_queries(
                provider, user.tenant_id,
                panel_ids=body.query_ids, namespace=body.namespace,
                search=body.search, since=body.since, limit=body.limit,
                actor_id=user.id, cluster=cluster,
            )
    except obs.ObsConsoleError as e:
        raise HTTPException(400, str(e)) from e
    return ObsQueryRunOut(
        results=[
            ObsQueryResultOut(id=r.id, query=r.query, ok=r.ok,
                              output=r.output, error=r.error)
            for r in results
        ]
    )


class ObsInstallOut(BaseModel):
    applied: list[str]
    finalizing: bool = False


@router.post("/{provider}/install", response_model=ObsInstallOut)
async def install_provider(
    provider: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """One-click install of an observability store into the tenant's cluster.

    Applies the vendored manifests over the tenant's registered kubeconfig
    (the tunnel-reachable customer cluster) and upserts the provider's
    Integration row so the daalu-native console lights up once the pods are
    Ready. Grafana additionally spawns a background finalizer that mints the
    /dashboards service-account token.
    """
    from daalu_automation.core.observability_install import (
        ObsInstallError,
        install,
    )

    try:
        result = await install(db, user.tenant_id, provider)
    except ObsInstallError as e:
        raise HTTPException(400, str(e)) from e
    return ObsInstallOut(**result)
