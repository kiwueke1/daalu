"""IT / Infrastructure / SRE domain tables."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class IncidentSeverity(str, enum.Enum):
    sev1 = "sev1"
    sev2 = "sev2"
    sev3 = "sev3"
    sev4 = "sev4"


class IncidentStatus(str, enum.Enum):
    open = "open"
    investigating = "investigating"
    mitigated = "mitigated"
    resolved = "resolved"


class Service(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "infra_services"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    owner_team: Mapped[str | None] = mapped_column(String(128))
    tier: Mapped[str] = mapped_column(String(16), default="tier-2", nullable=False)
    # Free-form tags ("region:us-east", "cluster:cluster-east-3").
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)


class Incident(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "infra_incidents"

    service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("infra_services.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    severity: Mapped[IncidentSeverity] = mapped_column(
        SAEnum(IncidentSeverity, name="incident_severity"),
        default=IncidentSeverity.sev3,
        nullable=False,
        index=True,
    )
    status: Mapped[IncidentStatus] = mapped_column(
        SAEnum(IncidentStatus, name="incident_status"),
        default=IncidentStatus.open,
        nullable=False,
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # AI-derived likely root cause + remediation steps.
    ai_root_cause: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ai_remediation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Linked telemetry/log snippets the AI used.
    evidence: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
