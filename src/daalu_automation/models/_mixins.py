"""Mixins shared by every model — id, tenant scoping, timestamps."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.config import DEFAULT_TENANT_ID


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


class TenantScopedMixin:
    """Every operational row belongs to a tenant.

    Multi-tenant enforcement (Postgres RLS) is deferred to Phase 2. Until
    then, callers stamp rows with ``DEFAULT_TENANT_ID`` and the tenant_id
    column is just a forward-compat slot.
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        default=DEFAULT_TENANT_ID,
        index=True,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )
