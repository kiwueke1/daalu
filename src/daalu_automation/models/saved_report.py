"""SavedReport — a named, persistable Reports → Query definition.

The Query tab's "Save" button writes one of these. The query body is the
exact JSON the user could POST to ``/reports/query`` — so loading a saved
report into the builder is symmetric with running it directly.

``pinned`` is admin-controlled and surfaces the report on the Home page
(via a dashboard tile). Non-admins can author and update their own
reports but cannot pin.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class SavedReport(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "saved_reports"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Pydantic QueryRequest serialized — {entity, filters, since_hours, limit, display}.
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # NULL for system-seeded examples (clonable from the UI).
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
