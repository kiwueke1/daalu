from __future__ import annotations

import enum

from sqlalchemy import JSON, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class RecommendationStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    dismissed = "dismissed"
    executed = "executed"


class Recommendation(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """An AI-generated next action surfaced to humans.

    Each recommendation is a single concrete suggestion — "call Acme
    before noon", "scale payment-service nodes", "schedule demo with
    Delta Health". The UI exposes Assign / Notify / Approve / Dismiss on
    every card; agents read the status to drive workflow execution.
    """

    __tablename__ = "recommendations"

    module: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[RecommendationStatus] = mapped_column(
        SAEnum(RecommendationStatus, name="recommendation_status"),
        default=RecommendationStatus.pending,
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    suggested_action: Mapped[str] = mapped_column(String(255), nullable=False)
    # 0..1 — how confident the AI is. Drives the colour gradient on the
    # recommendation card in the UI.
    confidence: Mapped[float] = mapped_column(default=0.0, nullable=False)
    # Free-form context the action handler needs (lead_id, service_name, …).
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
