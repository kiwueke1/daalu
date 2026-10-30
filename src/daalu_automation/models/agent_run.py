from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class AgentRun(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """One pass of one agent — captured so the Agents page can show
    last-action / success-rate / current-task per agent.
    """

    __tablename__ = "agent_runs"

    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # "ok", "error", "running"
    status: Mapped[str] = mapped_column(String(32), default="ok", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Short human-readable activity description — what the agent card shows.
    activity: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # Per-run metrics: events_processed, actions_taken, llm_tokens, etc.
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
