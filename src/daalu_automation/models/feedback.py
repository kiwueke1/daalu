"""Inbox for the /help feedback form.

Append-only — the platform never edits feedback rows, just lists them
for superusers (and eventually pipes them to Slack/email). user_id is
nullable so we can keep historical feedback if the user is deleted.
"""

from __future__ import annotations

import uuid as _uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class Feedback(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "feedback"

    user_id: Mapped[_uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Free-form short category, e.g. "bug", "idea", "praise". Default
    # "general" so callers don't have to think about it.
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="general")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # Context captured from the browser so we can reproduce. Both
    # truncated to 512 chars to bound storage.
    page_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(512), nullable=False, default="")
