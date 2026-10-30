"""Tunnel-health beat task.

Each tick:

1. Find the ``daalu-wireguard-hub`` pod in the same namespace as us.
2. Exec ``wg show wg0 dump`` and parse the per-peer latest-handshake
   timestamps. ``wg show ... dump`` is the script-friendly variant — one
   line per peer, tab-separated:

     <pubkey> <psk> <endpoint> <allowed-ips> <latest-handshake> <rx> <tx> <keepalive>

   ``latest-handshake`` is a Unix epoch (0 if there has never been one).
3. For every ClusterTunnel row whose ``customer_pubkey`` appears in the
   dump, update ``last_handshake_at`` and bucket the row's status by age
   against the windows in settings:

   - ≤ wireguard_connected_window_s seconds: ``connected``
   - ≤ wireguard_degraded_window_s seconds:  ``degraded``
   - otherwise:                              ``error``

4. Re-sync the hub Secret if the rendered config drifted (DB has a
   peer the Secret doesn't, or vice versa). This is the self-healing
   leg of the design: even if a ``POST /clusters/{slug}/callback``
   sync_hub_secret call previously failed transiently, the next beat
   tick converges the Secret with the DB.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from daalu_automation.config import get_settings
from daalu_automation.core.wireguard import (
    HubSecretUnavailable,
    _load_in_cluster_core_v1,
    _resolve_namespace,
    sync_hub_secret,
)
from daalu_automation.database import AsyncSessionLocal, engine
from daalu_automation.models import ClusterTunnel, ClusterTunnelStatus
from daalu_automation.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)


# ── kube exec helpers ───────────────────────────────────────────────────


def _find_hub_pod(core_v1, namespace: str) -> str | None:
    """Return the running hub pod name, or None if none is healthy."""
    pods = core_v1.list_namespaced_pod(
        namespace,
        label_selector="app=daalu-wireguard-hub",
    )
    for p in pods.items:
        if p.status.phase == "Running":
            return p.metadata.name
    return None


def _exec_wg_show(core_v1, kube_client, namespace: str, pod_name: str) -> str:
    """Run ``wg show wg0 dump`` in the hub pod and return its stdout.

    ``stream`` is a sync generator from the kubernetes client; this is
    intentionally called from a thread (see ``poll_tunnel_health``).
    """
    from kubernetes.stream import stream  # type: ignore

    return stream(
        core_v1.connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        container="hub",
        command=["wg", "show", "wg0", "dump"],
        stderr=False,
        stdin=False,
        stdout=True,
        tty=False,
        _preload_content=True,
    )


def parse_wg_dump(dump: str) -> dict[str, int]:
    """Map customer pubkey → latest_handshake epoch.

    The first line of ``wg show ... dump`` is the interface itself
    (private_key, public_key, listen_port, fwmark) — skipped.
    """
    out: dict[str, int] = {}
    lines = [ln for ln in dump.strip().splitlines() if ln.strip()]
    for line in lines[1:]:  # skip the interface line
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        pubkey = parts[0]
        try:
            ts = int(parts[4])
        except ValueError:
            continue
        out[pubkey] = ts
    return out


# ── core async loop ─────────────────────────────────────────────────────


async def _poll_once() -> dict[str, int]:
    """One pass: dump the hub, update DB rows, re-sync Secret if needed.

    Returns a small status dict for the celery task return value
    (mostly useful for ``celery events`` debugging).
    """
    settings = get_settings()
    try:
        ns = _resolve_namespace()
        core_v1, kube_client = _load_in_cluster_core_v1()
    except HubSecretUnavailable as e:
        logger.warning("wireguard.health.no_cluster", error=str(e))
        return {"observed": 0, "updated": 0, "reason": "no_cluster"}

    def _dump_blocking() -> tuple[str | None, str]:
        pod_name = _find_hub_pod(core_v1, ns)
        if pod_name is None:
            return None, ""
        try:
            text = _exec_wg_show(core_v1, kube_client, ns, pod_name)
        except Exception as e:  # noqa: BLE001
            logger.warning("wireguard.health.exec_failed", error=str(e))
            return pod_name, ""
        return pod_name, text

    pod_name, dump = await asyncio.to_thread(_dump_blocking)
    if pod_name is None:
        logger.info("wireguard.health.no_hub_pod")
        return {"observed": 0, "updated": 0, "reason": "no_hub_pod"}

    handshakes = parse_wg_dump(dump)
    logger.info("wireguard.health.dump", peers=len(handshakes), pod=pod_name)

    now = datetime.now(tz=timezone.utc)
    updated = 0
    async with AsyncSessionLocal() as db:
        # Only rows that have a customer pubkey are observable in the
        # dump. Pending rows are by definition not yet on the hub.
        rows = (
            await db.execute(
                select(ClusterTunnel).where(ClusterTunnel.customer_pubkey.is_not(None))
            )
        ).scalars().all()

        for row in rows:
            ts = handshakes.get(row.customer_pubkey or "")
            new_status = _classify(
                ts,
                connected_window=settings.wireguard_connected_window_s,
                degraded_window=settings.wireguard_degraded_window_s,
                now=now,
            )
            new_last = (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                if ts and ts > 0
                else row.last_handshake_at
            )
            if row.status != new_status or row.last_handshake_at != new_last:
                row.status = new_status
                row.last_handshake_at = new_last
                updated += 1
        if updated:
            await db.commit()

        # Self-healing: if any live row was added/removed since the last
        # sync, re-render. Cheap idempotent call — the patcher writes
        # the same body if nothing changed.
        try:
            await sync_hub_secret(
                db,
                listen_port=settings.wireguard_listen_port,
                hub_address=settings.wireguard_hub_address,
            )
        except HubSecretUnavailable as e:
            logger.warning("wireguard.health.sync_skipped", error=str(e))

    return {"observed": len(handshakes), "updated": updated}


def _classify(
    ts: int | None,
    *,
    connected_window: int,
    degraded_window: int,
    now: datetime,
) -> ClusterTunnelStatus:
    """Bucket a peer's last-handshake epoch into a status.

    ``ts`` semantics:
    - None: the pubkey wasn't seen in the dump at all. The hub doesn't
      know about this peer yet — stays in awaiting_handshake.
    - 0: the peer is in the config but no handshake has ever completed.
      Same bucket.
    - >0: a real handshake; compare age against windows.
    """
    if not ts:
        return ClusterTunnelStatus.awaiting_handshake
    age = (now - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds()
    if age <= connected_window:
        return ClusterTunnelStatus.connected
    if age <= degraded_window:
        return ClusterTunnelStatus.degraded
    return ClusterTunnelStatus.error


# ── celery glue ─────────────────────────────────────────────────────────


@celery_app.task(name="wireguard.poll_tunnel_health")
def poll_tunnel_health_task() -> dict[str, int]:
    # SQLAlchemy's async engine pins its connection pool to the first
    # event loop that touches it. `asyncio.run` creates a fresh loop
    # every tick, so without dispose() the second-and-onward calls
    # raise "Future ... attached to a different loop" from the pool's
    # cached futures. Dispose at end-of-tick → next tick gets a fresh
    # pool bound to its own loop. ~one Postgres reconnect / 30s, fine.
    async def _wrapped() -> dict[str, int]:
        try:
            return await _poll_once()
        finally:
            await engine.dispose()
    return asyncio.run(_wrapped())
