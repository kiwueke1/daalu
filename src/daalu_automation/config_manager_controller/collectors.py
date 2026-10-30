"""Controller-owned telemetry collectors for NV-CM environments.

The network-device telemetry pipeline has two halves:

* **Device-side** config (rsyslog forward + node_exporter) — pushed by the
  hub's "Connect monitoring" wizard (``POST /sot/devices/monitoring/connect``).
* **Cluster-side collectors** — the Grafana Alloy syslog receiver the devices
  forward to (hostNetwork :5514 → the tenant's Loki). Without it the wizard
  points every device at a black hole.

The collectors are *environment infrastructure*, exactly like the NV-CM
release itself: they must exist in every environment's cluster, and they must
be re-converged when a cluster is rebuilt — a one-off hand-apply (how the
gpu-node-3 lab started) silently disappears on reinstall. So the config-manager
controller owns them: every reconcile tick of an ``active`` environment
ensures they exist, cached by content hash so the 30s loop doesn't spam the
target cluster's API server.

The Prometheus ScrapeConfig for the device node_exporters is deliberately
NOT here — its target list is derived from the SoT inventory, which the hub
owns; the Connect-monitoring endpoint upserts it (see
``api/routers/sot_devices.py::monitoring_connect``).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any

import structlog
import yaml
from sqlalchemy import select

from daalu_automation.models import Integration

logger = structlog.get_logger(__name__)

_ASSETS = Path(__file__).resolve().parent.parent / "deploy_assets" / "telemetry"

# Default matches the loki chart Daalu documents for customer clusters; a
# tenant `loki` integration row (its in-cluster URL) overrides it.
_DEFAULT_LOKI_URL = "http://loki-gateway.monitoring.svc.cluster.local"

# (row_id) → sha256 of the docs last applied by THIS process. Keeps the 30s
# reconcile loop from re-applying unchanged manifests every tick; a controller
# restart re-applies once, which is exactly the heal-on-drift we want.
_applied_hash: dict[uuid.UUID, str] = {}


def render_collector_docs(loki_url: str) -> list[dict[str, Any]]:
    raw = (_ASSETS / "alloy-syslog.yaml").read_text()
    raw = raw.replace(
        "__LOKI_PUSH_URL__", f"{loki_url.rstrip('/')}/loki/api/v1/push"
    )
    return [
        d for d in yaml.safe_load_all(raw)
        if isinstance(d, dict) and d.get("kind")
    ]


async def _tenant_loki_url(db: Any, tenant_id: uuid.UUID) -> str:
    row = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.provider == "loki",
            )
        )
    ).scalar_one_or_none()
    url = ((row.config or {}).get("url") or "").strip() if row else ""
    return url or _DEFAULT_LOKI_URL


async def ensure_collectors(
    db: Any, row: Any, kubeconfig: dict[str, Any] | None
) -> None:
    """Idempotently apply the collector manifests to the env's cluster.

    Best-effort by design: a collector hiccup must never wedge the NV-CM
    reconcile (the release is the priority); failures log and retry next tick.
    """
    try:
        loki_url = await _tenant_loki_url(db, row.tenant_id)
        docs = render_collector_docs(loki_url)
        digest = hashlib.sha256(
            yaml.safe_dump(docs, sort_keys=True).encode()
        ).hexdigest()
        if _applied_hash.get(row.id) == digest:
            return
        from daalu_automation.core.kube_tools import _load_kube
        from daalu_automation.core.observability_install import _apply_docs_sync

        core_v1, _apps, _mod = _load_kube(tenant_kubeconfig=kubeconfig)
        applied = await asyncio.to_thread(
            _apply_docs_sync, core_v1.api_client, docs
        )
        _applied_hash[row.id] = digest
        logger.info(
            "config_manager_controller.collectors_ensured",
            tenant_id=str(row.tenant_id),
            row_id=str(row.id),
            applied=applied,
        )
    except Exception as e:  # noqa: BLE001 — never wedge the reconcile
        logger.warning(
            "config_manager_controller.collectors_failed",
            row_id=str(row.id),
            error=str(e)[:200],
        )
