from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class AlertSeverity(str, enum.Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class AlertStatus(str, enum.Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"
    suppressed = "suppressed"


class Alert(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """Operational alert surfaced to the Alerts page.

    Alerts are derived from events by agents — e.g. the infra agent
    promotes a critical Prometheus event into an Alert.
    """

    __tablename__ = "alerts"

    module: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[AlertSeverity] = mapped_column(
        SAEnum(AlertSeverity, name="alert_severity"),
        default=AlertSeverity.warning,
        nullable=False,
        index=True,
    )
    status: Mapped[AlertStatus] = mapped_column(
        SAEnum(AlertStatus, name="alert_status"),
        default=AlertStatus.open,
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # AI-derived confidence that this alert is actionable, 0..1.
    ai_confidence: Mapped[float] = mapped_column(default=0.0, nullable=False)
    # Optional pointer back to the source event that raised the alert.
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="SET NULL"), index=True
    )
    # Module-specific metadata (entity ids, dashboard links, …).
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # Stable hash that identifies "the same alert" across re-fires.
    # Built from (module, alert_name, key labels). NULL only for legacy
    # rows that pre-date the dedup feature.
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    # How many times the underlying signal has fired into this alert.
    # 1 on first fire, bumped on every dedup hit.
    occurrence_count: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, server_default="1"
    )
    # Timestamp of the most recent fire. Equal to created_at on first
    # fire; updated on every re-fire so the tile can sort by recency.
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertOccurrence(
    UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base
):
    """One row per individual fire of an alert.

    The first fire of a given fingerprint creates both an :class:`Alert`
    and a matching ``AlertOccurrence``. Subsequent fires (while the
    parent Alert is still open/acknowledged) only append a new
    ``AlertOccurrence`` and bump the parent's ``occurrence_count`` and
    ``last_seen_at`` — they do not create a duplicate Alert.

    The detail page reads this table to draw the "fired N times at …"
    timeline.
    """

    __tablename__ = "alert_occurrences"

    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("alerts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # When the underlying signal actually fired. Distinct from
    # ``created_at`` (which is the row insertion time) so we can backfill
    # historical fires accurately.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # Pointer back to the originating event (if any) — useful for
    # cross-linking on the timeline.
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="SET NULL")
    )
    # Snapshot of the payload metadata at fire time so the detail
    # timeline can show what was different between fires.
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
