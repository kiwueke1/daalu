"""Single-use invite tokens for adding users to a tenant.

Spec'd in book-customer §6 ("Inviting your team"). One row per
invite. Lifecycle derived from the timestamp + revoked_at columns:

  pending  → accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()
  expired  → accepted_at IS NULL AND revoked_at IS NULL AND expires_at <= now()
  revoked  → revoked_at IS NOT NULL  (terminal)
  accepted → accepted_at IS NOT NULL (terminal — accepted_user_id is the new User row)

Rows are never deleted on revoke / accept so the audit trail
survives.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class Invite(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "invites"

    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    # sha256(cleartext). The cleartext is shown to the inviter once in
    # the invite URL; never persisted in any form.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    invited_by_user_id: Mapped[_uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_user_id: Mapped[_uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
