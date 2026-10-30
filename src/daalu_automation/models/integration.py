from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class IntegrationStatus(str, enum.Enum):
    disconnected = "disconnected"
    connected = "connected"
    error = "error"


class Integration(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """A connector to an external system (monitoring, ticketing, cloud…).

    The actual credentials live in the platform secrets — this row just
    records which integrations a tenant has enabled, plus operator-supplied
    config (e.g. "which Prometheus base URL", "which Slack channel").
    """

    __tablename__ = "integrations"

    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[IntegrationStatus] = mapped_column(
        SAEnum(IntegrationStatus, name="integration_status"),
        default=IntegrationStatus.disconnected,
        nullable=False,
    )
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    # Stamped by the ``integrations.health_check`` beat task every tick
    # so the UI can render "last probed 47 s ago" alongside the status
    # badge. NULL means the row hasn't been probed yet (created since
    # the last beat tick, or beat is down).
    last_probed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # When set, the integration's URL is dialed through this cluster's
    # daalu-edge HTTP forward proxy instead of from the hub directly. Lets
    # the customer paste an in-cluster URL like
    # `http://prometheus.monitoring.svc.cluster.local:9090` without
    # exposing the service publicly. See core/cluster_proxy.py.
    cluster_tunnel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cluster_tunnels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
