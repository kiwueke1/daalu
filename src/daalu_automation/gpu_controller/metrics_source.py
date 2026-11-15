"""Connect the GPU cluster's Prometheus so AI Factory has metrics.

Add GPU installs dcgm-exporter (and its ServiceMonitor when the Prometheus
Operator is present), so the card's DCGM series land in *that cluster's*
Prometheus. AI Factory reads them through the tenant's ``prometheus``
Integration — and when nobody had created one, a GPU that was deployed and
serving rendered as an empty page. Once the GPU is active, the controller
finds the cluster's Prometheus Service and records it, bound to the cluster's
tunnel, so the card shows up with no extra step.

Never overwrites a Prometheus the tenant configured themselves.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from kubernetes_asyncio import client as k8s_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.models.integration import Integration, IntegrationStatus

logger = structlog.get_logger(__name__)

PROVIDER = "prometheus"

# Services whose name says "prometheus" but which aren't the query API.
_NOT_THE_SERVER = (
    "operator",
    "node-exporter",
    "alertmanager",
    "pushgateway",
    "adapter",
    "kube-state-metrics",
    "blackbox",
    "thanos",
    "coredns",
    "kubelet",
)


def pick_prometheus(services: list[dict[str, Any]]) -> str | None:
    """Choose the Prometheus query endpoint from a cluster's Services.

    Each service is ``{"name", "namespace", "cluster_ip", "ports": [int]}``.
    Returns an in-cluster URL or None. Prefers a normal ClusterIP Service
    over the headless ``prometheus-operated`` one the operator also creates
    (both work; the ClusterIP one is what a human would paste).
    """
    best: tuple[int, str] | None = None
    for s in services:
        name = s.get("name") or ""
        if "prometheus" not in name or any(x in name for x in _NOT_THE_SERVER):
            continue
        ports = s.get("ports") or []
        if 9090 not in ports:
            continue
        headless = (s.get("cluster_ip") or "None") == "None"
        score = 1 if headless else 0
        url = f"http://{name}.{s.get('namespace')}.svc.cluster.local:9090"
        if best is None or score < best[0]:
            best = (score, url)
    return best[1] if best else None


async def discover_prometheus_url(api: k8s_client.ApiClient) -> str | None:
    core = k8s_client.CoreV1Api(api)
    svcs = await core.list_service_for_all_namespaces()
    services = [
        {
            "name": s.metadata.name,
            "namespace": s.metadata.namespace,
            "cluster_ip": s.spec.cluster_ip,
            "ports": [p.port for p in (s.spec.ports or [])],
        }
        for s in svcs.items
    ]
    return pick_prometheus(services)


async def needs_prometheus(db: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """True when the tenant has no Prometheus URL configured at all."""
    row = await _existing(db, tenant_id)
    return row is None or not ((row.config or {}).get("url") or "").strip()


async def record_prometheus(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    cluster_tunnel_id: uuid.UUID,
    url: str,
) -> bool:
    """Create (or fill an empty) ``prometheus`` Integration. Returns True if written."""
    row = await _existing(db, tenant_id)
    if row is not None and ((row.config or {}).get("url") or "").strip():
        return False  # the tenant's own choice wins
    if row is None:
        row = Integration(
            tenant_id=tenant_id,
            provider=PROVIDER,
            # Same module/name the Integrations page writes for this provider
            # (modules/infra/integrations.py PrometheusAdapter).
            module="infra",
            name="Prometheus / Alertmanager",
            status=IntegrationStatus.connected,
            config={"url": url},
            cluster_tunnel_id=cluster_tunnel_id,
        )
        db.add(row)
    else:
        row.config = {**(row.config or {}), "url": url}
        row.cluster_tunnel_id = cluster_tunnel_id
        row.status = IntegrationStatus.connected
        row.last_error = None
    await db.commit()
    logger.info(
        "gpu_controller.prometheus_connected",
        tenant_id=str(tenant_id),
        url=url,
    )
    return True


async def _existing(db: AsyncSession, tenant_id: uuid.UUID) -> Integration | None:
    return (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.provider == PROVIDER,
            )
        )
    ).scalars().first()

