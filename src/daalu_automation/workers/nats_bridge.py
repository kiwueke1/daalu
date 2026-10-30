"""NATS→Redis event bridge for per-tenant NV-CM stacks.

NV-CM publishes lifecycle events on its NATS JetStream (workflow results,
device / Nautobot change). This bridge subscribes to a tenant's NV-CM NATS
(reached over the WireGuard tunnel) and republishes the relevant ones onto
the hub's Redis event bus (:func:`core.events.publish`) so existing agents
can confirm deploys and open drift proposals — closing the loop.

Runs as a long-lived subscriber (one task per tenant), suitable for a
dedicated process alongside ``daalu agents``. The NATS connect is
injectable so unit tests can drive ``_map_subject_to_event`` and the
republish path without a live NATS.

See docs/design/nv-config-manager-integration.md §9.3.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from sqlalchemy import select

from daalu_automation.core.events import EventEnvelope, publish
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import Integration

logger = structlog.get_logger(__name__)

# NV-CM JetStream subjects we bridge (defaults; overridable per tenant via
# the config_manager integration's ``nats_subjects`` config key).
DEFAULT_SUBJECTS = (
    "nv-config-manager.workflow.result",
    "nv-config-manager.nautobotchange",
    "nv-config-manager.devicechange",
)

# NV-CM subject → hub EventEnvelope type.
_SUBJECT_TYPE = {
    "nv-config-manager.workflow.result": "config_manager.workflow.result",
    "nv-config-manager.nautobotchange": "config_manager.device_change",
    "nv-config-manager.devicechange": "config_manager.device_change",
}


def _map_subject_to_event(
    tenant_id: str, subject: str, data: dict[str, Any]
) -> EventEnvelope | None:
    """Translate one NV-CM NATS message into a hub EventEnvelope.

    Returns ``None`` for subjects we don't bridge. A workflow result that
    failed is surfaced at ``warning`` severity so the infra agent can pick
    it up; everything else is informational.
    """
    etype = _SUBJECT_TYPE.get(subject)
    if etype is None:
        return None
    status = str(data.get("status", "")).upper()
    severity = "warning" if status in {"FAILED", "TIMED_OUT", "TERMINATED"} else "info"
    summary = data.get("summary") or f"NV-CM {subject.split('.')[-1]}"
    return EventEnvelope(
        tenant_id=tenant_id,
        type=etype,
        module="device_config",
        source="nv-config-manager",
        severity=severity,
        summary=summary,
        payload={"subject": subject, **data},
    )


async def _tenants_with_nats() -> list[tuple[str, str, list[str], str | None]]:
    """Return (tenant_id, nats_url, subjects, creds_path) for bridgeable tenants."""
    out: list[tuple[str, str, list[str], str | None]] = []
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Integration).where(
                    Integration.provider == "config_manager"
                )
            )
        ).scalars().all()
    for row in rows:
        cfg = row.config or {}
        nats_url = cfg.get("nats_url")
        if not nats_url:
            continue
        subjects = cfg.get("nats_subjects") or list(DEFAULT_SUBJECTS)
        out.append((str(row.tenant_id), nats_url, subjects, cfg.get("nats_creds_path")))
    return out


async def bridge_tenant(
    tenant_id: str,
    nats_url: str,
    subjects: list[str],
    *,
    creds_path: str | None = None,
    connect=None,
) -> None:
    """Subscribe to one tenant's NV-CM NATS and republish onto Redis.

    Long-lived: runs until cancelled. ``connect`` is injectable for tests
    (defaults to ``nats.connect``).
    """
    if connect is None:
        import nats  # imported lazily so the dep is only needed at runtime

        connect = nats.connect

    opts: dict[str, Any] = {"servers": [nats_url]}
    if creds_path:
        opts["user_credentials"] = creds_path
    nc = await connect(**opts)
    js = nc.jetstream()
    logger.info("nats_bridge.connected", tenant_id=tenant_id, nats_url=nats_url)

    async def _handler(msg: Any) -> None:
        try:
            data = json.loads(msg.data.decode())
        except Exception:  # noqa: BLE001 — non-JSON payloads are ignored
            data = {"raw": getattr(msg, "data", b"").decode(errors="replace")}
        env = _map_subject_to_event(tenant_id, msg.subject, data)
        if env is not None:
            await publish(env)
        ack = getattr(msg, "ack", None)
        if ack is not None:
            await ack()

    for subject in subjects:
        await js.subscribe(subject, cb=_handler, durable="daalu-bridge")
    # Block until cancelled.
    await asyncio.Event().wait()


async def run_all_bridges(*, connect=None) -> None:
    """Discover bridgeable tenants and run one subscriber task per tenant."""
    tenants = await _tenants_with_nats()
    if not tenants:
        logger.info("nats_bridge.no_tenants")
        return
    tasks = [
        asyncio.create_task(
            bridge_tenant(tid, url, subjects, creds_path=creds, connect=connect)
        )
        for (tid, url, subjects, creds) in tenants
    ]
    await asyncio.gather(*tasks)
