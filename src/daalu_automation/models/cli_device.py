"""CLI device-authorization rows — the `daalu login` browser round-trip.

Backs the RFC-8628-style device flow *and* the express "paste this command"
flow that the workspace page offers. Both converge on a single redeem step
(``POST /api/v1/cli/device/token``) that mints a normal ``dpat_`` personal
access token, so the CLI has exactly one code path.

A row is short-lived (10 min) and single-use:

* ``daalu login`` → ``status='pending'`` until the user approves in the browser
  (``status='approved'``), then the CLI redeems it (``status='redeemed'``).
* the workspace page's express command creates a row already ``approved`` for
  the logged-in user, so the CLI skips straight to redeem.

The cleartext ``device_code`` is the secret the CLI holds; only its sha256 hash
is stored, so a DB leak can't be redeemed. ``user_code`` is the short,
human-typable code shown in the browser.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import TimestampMixin, UUIDPrimaryKeyMixin

# Lifecycle states. Kept as plain strings (not an enum type) to match the
# rest of the schema's lightweight convention.
DEVICE_PENDING = "pending"
DEVICE_APPROVED = "approved"
DEVICE_REDEEMED = "redeemed"
DEVICE_DENIED = "denied"


class CliDeviceAuthorization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "cli_device_authorizations"

    # sha256 hex of the cleartext device_code the CLI holds. Unique so a
    # redeem is an unambiguous single-row lookup.
    device_code_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    # Short human code shown in the browser (``XXXX-XXXX``). Stored
    # upper-cased; looked up case-insensitively by the activate page.
    user_code: Mapped[str] = mapped_column(
        String(16), nullable=False, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DEVICE_PENDING
    )
    # Set at approval time — the row starts anonymous (the CLI has no
    # session; the browser supplies the identity).
    tenant_id: Mapped[_uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
    )
    user_id: Mapped[_uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    # The PAT minted at redeem time. Kept for audit / so we never mint twice.
    pat_id: Mapped[_uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("personal_access_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Label the CLI sends, e.g. "daalu-cli @ hostname" — becomes the PAT name.
    client_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
