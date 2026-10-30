"""ReportSchedule — a cron schedule for delivering a SavedReport.

One row per scheduled delivery. The beat task ``reports.dispatch_due``
runs every minute, finds rows with ``next_run_at <= now`` and ``enabled``,
runs the saved query, formats the result, dispatches it through
``core.notify``, then advances ``next_run_at`` to the next fire time.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class ReportSchedule(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "report_schedules"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    saved_report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("saved_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 5-field crontab. Validated against celery.schedules.crontab on
    # write so we never persist a non-parseable expression.
    cron: Mapped[str] = mapped_column(String(64), nullable=False)
    # "slack" or "email".
    destination: Mapped[str] = mapped_column(String(16), nullable=False)
    # Slack channel ("#netops"), email address ("ops@..."), or empty for
    # the tenant default Slack channel.
    recipient: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # "markdown" or "csv". CSV gets attached as a file (best-effort —
    # the Slack adapter only sends text right now, so CSV-to-Slack
    # falls back to a Markdown table).
    fmt: Mapped[str] = mapped_column(
        "format", String(16), default="markdown", nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
