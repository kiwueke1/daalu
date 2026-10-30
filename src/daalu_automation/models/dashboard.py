"""Dashboard — a grid of tiles, each backed by a SavedReport.

A dashboard tile is a tiny pointer ({saved_report_id, render, w, h, x, y}).
The renderable rows live on the SavedReport's definition, so a dashboard
doesn't duplicate any query state — moving a query elsewhere updates
every dashboard that references it.

``home_pinned`` is admin-controlled and surfaces the dashboard's tiles
on the Home page; the layout there picks the dashboard with
``home_pinned=true``, if any.
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


class Dashboard(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "dashboards"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Tile shape (per element):
    # {"saved_report_id": uuid, "render": "table"|"number"|"line"|"bar"|"pie",
    #  "w": int, "h": int, "x": int, "y": int, "title": str | null}
    tiles: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    home_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
