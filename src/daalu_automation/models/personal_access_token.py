"""Personal access tokens — opaque API keys minted from /settings.

Authenticates scripts/CI calling the daalu API. The cleartext token is
shown to the user exactly once at creation (POST /auth/tokens); after
that only the sha256 hash is persisted, so a database leak does not
expose anyone's tokens. Lookup at request time hashes the bearer value
and matches against ``token_hash``.

Tokens look like ``dpat_<24 random url-safe bytes>``. The ``prefix``
column stores the first 12 characters so the UI can show a human
identifier in the token list without keeping the secret around.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class PersonalAccessToken(
    UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base
):
    __tablename__ = "personal_access_tokens"

    user_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # sha256 hex digest of the cleartext token. Unique so a collision
    # (vanishingly unlikely with 24 random bytes) is a 500, not an
    # ambiguous lookup.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    # First chars of the cleartext token, displayed in the list view so
    # the user can recognise which row matches which token they have
    # saved elsewhere.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
