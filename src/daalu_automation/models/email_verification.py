"""Single-use email-verification tokens for self-service signup.

One row per verification link. Mirrors :class:`Invite`'s token model:
the cleartext (``dver_<random>``) is emailed to the new user exactly
once; only the sha256 hash is stored in :attr:`token_hash`.

Lifecycle derived from the timestamp columns:

  pending   → consumed_at IS NULL AND expires_at > now()
  expired   → consumed_at IS NULL AND expires_at <= now()
  consumed  → consumed_at IS NOT NULL  (terminal — the account is verified)

Rows are never deleted so the audit trail survives. A "resend" issues a
*new* row rather than mutating an old one.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import TimestampMixin, UUIDPrimaryKeyMixin


class EmailVerificationToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "email_verification_tokens"

    user_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # sha256(cleartext). The cleartext is only ever in the emailed link.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
