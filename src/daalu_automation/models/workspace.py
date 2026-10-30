"""Browser-IDE workspace, one per (tenant, user).

The workspace-controller reconciles K8s state (a Deployment + PVC for
this row) against the desired ``state`` column. Lifecycle:

    provisioning  -- pod scheduled, init container running
    active        -- code-server reachable, user has an open session
    paused        -- scaled to 0 after 24h idle (PVC retained)
    destroyed     -- PVC released; row kept for audit
    error         -- reconcile failed; needs manual intervention

The ``last_heartbeat_at`` column is updated by the IDE proxy on
every successful WebSocket frame. The reconciler reads it to decide
when to pause / destroy.
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


class Workspace(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"

    user_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state: Mapped[str] = mapped_column(String(32), default="provisioning", nullable=False)
    profile: Mapped[str] = mapped_column(String(16), default="small", nullable=False)
    # Catalog id of the coding model the assistant in this workspace uses
    # (``core.model_catalog``). Nullable for rows predating the column;
    # the controller injects ``DAALU_MODEL`` from the resolved served name,
    # falling back to the catalog default when null.
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pod_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pvc_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    git_repo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    git_branch: Mapped[str] = mapped_column(String(255), default="main", nullable=False)
    # Personal-access-token for cloning a *private* repo (GitHub / GitLab /
    # self-hosted), encrypted at rest with ``core.crypto`` (Fernet, keyed off
    # ``settings.secret_key``). Never returned to the browser — the API only
    # ever exposes a boolean ``git_authenticated``. The reconciler decrypts it
    # to materialise a short-lived per-workspace K8s Secret that backs the
    # init-container's git credential helper. Nullable: public repos and
    # empty workspaces store nothing here.
    git_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    destroyed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
