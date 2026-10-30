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


class WorkflowRunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    waiting_for_approval = "waiting_for_approval"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class WorkflowRun(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """An execution of a named automation.

    Workflows are pluggable Python coroutines registered by modules (see
    ``core/workflows.py``); this table just records each invocation so
    the Automations page can show real history.
    """

    __tablename__ = "workflow_runs"

    workflow_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[WorkflowRunStatus] = mapped_column(
        SAEnum(WorkflowRunStatus, name="workflow_run_status"),
        default=WorkflowRunStatus.pending,
        nullable=False,
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    output_payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)

    # The alert this run remediates, when it was kicked from "Approve & run".
    # Null for code-registered automations. Lets the Workflows page link each
    # run back to its source alert.
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("alerts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Ordered record of each tool the agent ran in this workflow:
    # ``[{order, kind, title, tool, input, output, status}]``. Drives the
    # workflow detail page's step-by-step view.
    steps: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
