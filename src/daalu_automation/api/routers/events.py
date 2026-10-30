"""Event feed — list + live SSE stream."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from daalu_automation.api.deps import current_tenant_id, verify_ingest_key
from daalu_automation.api.schemas import EventOut
from daalu_automation.config import get_settings
from daalu_automation.core.events import EventEnvelope, publish
from daalu_automation.database import get_db
from daalu_automation.models import Event

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[EventOut])
async def list_events(
    *,
    module: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    since_hours: int = Query(default=24, ge=1, le=24 * 14),
    limit: int = Query(default=100, le=500),
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=since_hours)
    stmt = (
        select(Event)
        .where(Event.tenant_id == tenant_id, Event.occurred_at >= cutoff)
        .order_by(desc(Event.occurred_at))
        .limit(limit)
    )
    if module:
        stmt = stmt.where(Event.module == module)
    if severity:
        stmt = stmt.where(Event.severity == severity)
    rows = (await db.execute(stmt)).scalars().all()
    return rows


@router.post("", response_model=EventOut, status_code=202)
async def emit_event(
    envelope: dict,
    tenant_id=Depends(verify_ingest_key),
):
    """Generic ingestion endpoint — webhooks point here.

    Body shape matches ``EventEnvelope``. Tenant is resolved from the
    sha256 of ``X-Daalu-Key`` against ``tenants.ingest_api_key_hash``;
    the caller cannot override it via the request body (any
    ``tenant_id`` field is dropped before construction).
    """
    envelope.pop("tenant_id", None)
    envelope["tenant_id"] = str(tenant_id)
    ev = EventEnvelope(**envelope)
    await publish(ev)
    return EventOut(
        id=ev.event_id,
        type=ev.type,
        module=ev.module,
        source=ev.source,
        severity=ev.severity,
        summary=ev.summary,
        occurred_at=datetime.fromisoformat(ev.occurred_at),
        payload=ev.payload,
    )


@router.get("/stream")
async def stream_events(
    module: str | None = None,
    tenant_id=Depends(current_tenant_id),
):
    """Server-Sent Events stream the UI subscribes to.

    Backed by a Redis pub/sub-style poll of the event stream so the API
    pod stays stateless — multiple frontend tabs can each open their
    own stream without cross-coordination.
    """
    settings = get_settings()

    async def _gen():
        from redis.asyncio import Redis

        r = Redis.from_url(settings.redis_url, decode_responses=True)
        last_id = "$"
        while True:
            try:
                resp = await r.xread(
                    {settings.event_stream_key: last_id}, count=16, block=15_000
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)
                continue
            if not resp:
                # Heartbeat — keeps proxies from closing idle SSE connections.
                yield {"event": "ping", "data": "{}"}
                continue
            for _stream, messages in resp:
                for stream_id, fields in messages:
                    last_id = stream_id
                    if module and fields.get("module") != module:
                        continue
                    if fields.get("tenant_id") != str(tenant_id):
                        continue
                    yield {
                        "event": "operational-event",
                        "data": json.dumps(
                            {
                                "id": fields.get("event_id"),
                                "type": fields["type"],
                                "module": fields["module"],
                                "source": fields["source"],
                                "severity": fields.get("severity", "info"),
                                "summary": fields.get("summary", ""),
                                "occurred_at": fields.get("occurred_at"),
                                "payload": json.loads(fields.get("payload_json", "{}")),
                            }
                        ),
                    }

    return EventSourceResponse(_gen())
