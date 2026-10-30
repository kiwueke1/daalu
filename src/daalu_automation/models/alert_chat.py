"""Per-alert chat + remediation action ledger.

When a user opens an alert tile in the UI, they get a chat panel scoped
to that alert. The LLM may propose tool calls (read-only kube queries,
or write actions like ``rollout_undo``). Read tools auto-run; write
tools land as pending ``AlertAction`` rows the user must approve.

Two tables back the feature:

* :class:`AlertChatMessage` — chronological message log. Role is
  user / assistant / tool, matching Anthropic's content-block schema so
  we can replay the full transcript back to Claude on every turn.
* :class:`AlertAction` — one row per proposed tool call. ``status``
  walks pending → approved → executed (or rejected / failed) and the
  row carries the captured stdout/stderr once executed.
"""

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


class ChatRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    tool = "tool"


class AlertChatMessage(
    UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base
):
    __tablename__ = "alert_chat_messages"

    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("alerts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[ChatRole] = mapped_column(
        SAEnum(ChatRole, name="alert_chat_role"), nullable=False
    )
    # Plain-text content. For assistant messages with tool_use blocks
    # this still captures whatever prose Claude returned alongside the
    # tool call (often a 1-sentence rationale).
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Mirror of Anthropic's tool_use blocks for assistant messages:
    # [{"id": "toolu_…", "name": "get_pod_logs", "input": {…}}, …].
    # Empty list for messages without tool calls.
    tool_calls_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # When ``role`` is ``tool``, this points at the assistant message's
    # tool_use id so we can replay the exchange back to the model.
    tool_call_id: Mapped[str | None] = mapped_column(String(64))


class ActionStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    executed = "executed"
    failed = "failed"


class AlertAction(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """A single proposed-or-executed tool call attached to an alert chat.

    Read-only tools (``get_pod_logs``, ``describe_pod``, …) auto-execute
    and land directly in ``executed``. Write tools (``rollout_undo``,
    ``scale_deployment``, …) land as ``pending`` until the user clicks
    Approve in the chat panel.
    """

    __tablename__ = "alert_actions"

    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("alerts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The assistant message whose tool_use block this action implements.
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("alert_chat_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Anthropic's tool_use id ("toolu_…") so we can wire the tool_result
    # back to the same id when replaying.
    tool_call_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_input: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # ``True`` for write-capable tools; the UI surfaces an Approve/Reject
    # card and the executor refuses to run until the user clicks one.
    requires_approval: Mapped[bool] = mapped_column(default=False, nullable=False)
    status: Mapped[ActionStatus] = mapped_column(
        SAEnum(ActionStatus, name="alert_action_status"),
        default=ActionStatus.pending,
        nullable=False,
        index=True,
    )
    # Captured stdout/stderr (or exception message) from the tool runner.
    result_output: Mapped[str] = mapped_column(Text, default="", nullable=False)
    result_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
