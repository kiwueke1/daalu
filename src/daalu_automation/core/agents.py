"""Base class for AI operational agents.

An agent is a long-running coroutine that:

1. subscribes to the event stream (or runs on a schedule),
2. decides what to do — using the LLM as needed,
3. emits derived events (alerts, recommendations, follow-up workflows).

Concrete agents live under ``modules/<name>/agent.py``. Registering an
agent at import time (``register_agent(...)``) makes it discoverable by
the worker bootstrap and by the Agents page.
"""

from __future__ import annotations

import abc
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from daalu_automation.config import DEFAULT_TENANT_ID
from daalu_automation.core.events import EventEnvelope, subscribe

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class AgentDescriptor:
    name: str
    module: str
    description: str
    # Event types this agent reacts to. Use "*" to consume everything for
    # the agent's module.
    subscribed_event_types: tuple[str, ...]


class Agent(abc.ABC):
    """Implement ``handle`` to process one event at a time."""

    descriptor: AgentDescriptor

    def __init__(self) -> None:
        self.log = structlog.get_logger(__name__).bind(agent=self.descriptor.name)

    @abc.abstractmethod
    async def handle(self, event: EventEnvelope) -> None:  # pragma: no cover
        ...

    def should_handle(self, event: EventEnvelope) -> bool:
        types = self.descriptor.subscribed_event_types
        if "*" in types:
            return event.module == self.descriptor.module
        return event.type in types

    async def run_forever(self) -> None:
        """Main loop — call from a Celery worker / k8s deployment."""
        consumer = f"{self.descriptor.name}-{uuid.uuid4().hex[:8]}"
        self.log.info("agent.starting", consumer=consumer)
        async for _stream_id, event in subscribe(consumer):
            if not self.should_handle(event):
                continue
            # Daalu Private: skip tenants that opted into edge-agents.
            # Their dedicated edge pod picks the event up by polling
            # the hub's /api/v1/internal/agent-tasks endpoint. Running
            # the loop here too would cause duplicate side-effects.
            if not await _should_handle_on_hub(event.tenant_id):
                self.log.debug(
                    "agent.skipping_edge_tenant",
                    tenant_id=event.tenant_id,
                    event_type=event.type,
                )
                continue
            run_id = uuid.uuid4()
            started = datetime.now(tz=timezone.utc)
            await self._record_run_start(run_id, started, event)
            try:
                await self.handle(event)
                await self._record_run_end(run_id, status="ok", error=None)
            except Exception as e:  # noqa: BLE001
                self.log.exception("agent.handle_failed", event_type=event.type)
                await self._record_run_end(run_id, status="error", error=str(e))

    async def _record_run_start(
        self, run_id: uuid.UUID, started: datetime, event: EventEnvelope
    ) -> None:
        from daalu_automation.database import AsyncSessionLocal
        from daalu_automation.models import AgentRun

        async with AsyncSessionLocal() as db:
            db.add(
                AgentRun(
                    id=run_id,
                    tenant_id=uuid.UUID(event.tenant_id),
                    agent_name=self.descriptor.name,
                    module=self.descriptor.module,
                    status="running",
                    started_at=started,
                    activity=f"processing {event.type}",
                )
            )
            await db.commit()

    async def _record_run_end(
        self, run_id: uuid.UUID, *, status: str, error: str | None
    ) -> None:
        from sqlalchemy import update

        from daalu_automation.database import AsyncSessionLocal
        from daalu_automation.models import AgentRun

        async with AsyncSessionLocal() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(
                    status=status,
                    finished_at=datetime.now(tz=timezone.utc),
                    error_message=error,
                )
            )
            await db.commit()


# ── Daalu Private: hub-side gating ───────────────────────────────────────
# Cached tenant lookups for the "should hub handle this?" check on the
# agent hot path. Tenants flip Private mode rarely, so a ~30s cache is
# more than safe and avoids one DB hit per event.
_EDGE_FLAG_CACHE: dict[str, tuple[float, bool]] = {}
_EDGE_FLAG_TTL_S = 30.0


async def _should_handle_on_hub(tenant_id: str) -> bool:
    """Return False for tenants whose edge pod owns the agent loop.

    Cache misses cost one DB hit. Cache hits are O(1). The cache is
    process-local — restarting the agent pod flushes it, which is
    acceptable: Private flips are rare and propagation within 30 s
    is fast enough.
    """
    import time as _time

    from sqlalchemy import select

    from daalu_automation.database import AsyncSessionLocal
    from daalu_automation.models.tenant import Tenant

    now = _time.monotonic()
    cached = _EDGE_FLAG_CACHE.get(tenant_id)
    if cached and now - cached[0] < _EDGE_FLAG_TTL_S:
        return not cached[1]  # cached value is edge_agents_enabled
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none()
    edge_enabled = bool(getattr(row, "edge_agents_enabled", False))
    _EDGE_FLAG_CACHE[tenant_id] = (now, edge_enabled)
    return not edge_enabled


# ── Registry — modules call register_agent() at import time ──────────────
_AGENT_FACTORIES: dict[str, Callable[[], Agent]] = {}


def register_agent(factory: Callable[[], Agent]) -> Callable[[], Agent]:
    """Decorator to register an agent factory by its descriptor name."""
    agent = factory()
    _AGENT_FACTORIES[agent.descriptor.name] = factory
    logger.info(
        "agent.registered",
        name=agent.descriptor.name,
        module=agent.descriptor.module,
    )
    return factory


def list_agents() -> list[AgentDescriptor]:
    return [factory().descriptor for factory in _AGENT_FACTORIES.values()]


def get_agent(name: str) -> Agent:
    return _AGENT_FACTORIES[name]()


async def emit_alert(
    *,
    module: str,
    title: str,
    body: str,
    severity: str = "warning",
    ai_confidence: float = 0.0,
    source_event_id: uuid.UUID | None = None,
    metadata: dict | None = None,
    tenant_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Helper used by agents to surface an Alert without importing models everywhere.

    Deduplicates re-fires of the same logical alert: when an
    *open* or *acknowledged* alert with the same ``(tenant, fingerprint)``
    already exists, this appends an :class:`AlertOccurrence` and bumps
    the parent's counters instead of creating a new Alert row. Resolved
    alerts that re-fire create a fresh Alert — once an operator has
    closed an incident it stays closed.
    """
    from sqlalchemy import select

    from daalu_automation.core.alert_fingerprint import compute_fingerprint
    from daalu_automation.database import AsyncSessionLocal
    from daalu_automation.models import (
        Alert,
        AlertOccurrence,
        AlertSeverity,
        AlertStatus,
    )

    tenant = tenant_id or DEFAULT_TENANT_ID
    metadata = metadata or {}
    fingerprint = compute_fingerprint(
        module=module, title=title, metadata=metadata
    )
    now = datetime.now(tz=timezone.utc)

    async with AsyncSessionLocal() as db:
        existing_stmt = (
            select(Alert)
            .where(
                Alert.tenant_id == tenant,
                Alert.fingerprint == fingerprint,
                Alert.status.in_(
                    (AlertStatus.open, AlertStatus.acknowledged)
                ),
            )
            .order_by(Alert.created_at.desc())
            .limit(1)
        )
        existing = (await db.execute(existing_stmt)).scalar_one_or_none()

        if existing is not None:
            existing.occurrence_count = (existing.occurrence_count or 1) + 1
            existing.last_seen_at = now
            db.add(
                AlertOccurrence(
                    tenant_id=tenant,
                    alert_id=existing.id,
                    occurred_at=now,
                    source_event_id=source_event_id,
                    metadata_json=metadata,
                )
            )
            await db.commit()
            return existing.id

        alert = Alert(
            tenant_id=tenant,
            module=module,
            severity=AlertSeverity(severity),
            title=title,
            body=body,
            ai_confidence=ai_confidence,
            source_event_id=source_event_id,
            metadata_json=metadata,
            fingerprint=fingerprint,
            occurrence_count=1,
            last_seen_at=now,
        )
        db.add(alert)
        await db.flush()
        db.add(
            AlertOccurrence(
                tenant_id=tenant,
                alert_id=alert.id,
                occurred_at=now,
                source_event_id=source_event_id,
                metadata_json=metadata,
            )
        )
        await db.commit()
        await db.refresh(alert)
        return alert.id


async def emit_recommendation(
    *,
    module: str,
    title: str,
    rationale: str,
    suggested_action: str,
    confidence: float = 0.5,
    payload: dict | None = None,
    tenant_id: uuid.UUID | None = None,
) -> uuid.UUID:
    from daalu_automation.database import AsyncSessionLocal
    from daalu_automation.models import Recommendation

    async with AsyncSessionLocal() as db:
        rec = Recommendation(
            tenant_id=tenant_id or DEFAULT_TENANT_ID,
            module=module,
            title=title,
            rationale=rationale,
            suggested_action=suggested_action,
            confidence=confidence,
            payload=payload or {},
        )
        db.add(rec)
        await db.commit()
        await db.refresh(rec)
        return rec.id


# Type alias for tests / inline lambdas — a bare async function counts as an agent.
AgentHandler = Callable[[EventEnvelope], Awaitable[None]]
