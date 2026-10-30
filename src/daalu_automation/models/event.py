"""Event — the canonical record of something that happened.

Every other table (alerts, recommendations, briefings) is derived from
events: ingestion writes events, agents read events, briefings summarise
events. Keeping the source-of-truth in one place lets new modules plug
in by just emitting new event types.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class EventSeverity(str, enum.Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class Event(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "events"

    # Hierarchical type slug — "<module>.<noun>.<verb>" by convention.
    # Examples: "infra.incident.escalated", "infra.deploy.failed".
    type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # Originating module — infra, finance, … Used as a coarse filter
    # in the UI and on the briefing scheduler.
    module: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # System/integration that produced the event (e.g. "pagerduty",
    # "prometheus"). Kept separate from ``module`` so the same module
    # can fan out across many sources.
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[EventSeverity] = mapped_column(
        SAEnum(EventSeverity, name="event_severity"),
        default=EventSeverity.info,
        nullable=False,
    )
    # Human-readable one-line summary — what the UI feed renders without
    # decoding the payload.
    summary: Mapped[str] = mapped_column(String(512), nullable=False)
    # When the underlying business event occurred. Distinct from
    # ``created_at`` because ingestion can lag.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # Free-form structured payload — module-specific.
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
