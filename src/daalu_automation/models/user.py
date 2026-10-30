from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class User(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    hashed_password: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # When the account's email was confirmed. Self-service signups
    # (core/signup.py) are created inactive with this NULL; redeeming the
    # verification link sets it and flips is_active true. NULL for accounts that
    # never went through verification (invite redemption, SSO, seeded admins).
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Tenant-scoped admin (manages users/integrations of own tenant).
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Platform-level operator (manages tenants themselves). Distinct from
    # is_admin so a customer's tenant-admin cannot create or read other
    # tenants. Only superusers can hit /api/v1/tenants/*.
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Which module pages the user has subscribed to in their briefing.
    # Free-form list of module names ("infra", …) — kept as text so
    # adding a new module doesn't require a migration.
    briefing_modules: Mapped[str] = mapped_column(String(1024), default="infra", nullable=False)
    # Per-user UI preferences (theme, density, accent, per-channel
    # notification toggles, etc). Kept as a single JSONB blob because the
    # shape is volatile and the data is tiny — schema evolves on the
    # frontend without a migration. See `core.preferences.DEFAULT_PREFS`
    # for the canonical shape.
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
