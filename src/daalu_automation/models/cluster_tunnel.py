"""WireGuard tunnel state for an onboarded managed cluster.

One row per (tenant, cluster) pair. Holds:

- the keypair the operator hub uses to talk to this customer,
- the customer-side pubkey + tunnel IP assigned from 10.200.0.0/16,
- a one-shot invite token (sha256 hashed) the customer-side edge
  container uses to authenticate its bootstrap callback, and
- liveness metadata refreshed by the tunnel-health Celery beat task.

The kubeconfig itself still lives in the related ``Integration`` row
(``provider="kubernetes"``); this row owns the L3 connectivity that
makes that kubeconfig reachable.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class ClusterTunnelStatus(str, enum.Enum):
    # Row created; hub Secret not yet patched with the peer block.
    pending = "pending"
    # Peer is on the hub; waiting for the customer-side edge to connect.
    awaiting_handshake = "awaiting_handshake"
    # Handshake observed within the last 3 minutes.
    connected = "connected"
    # Handshake observed but stale (3-10 min). UI shows yellow.
    degraded = "degraded"
    # No handshake for >10 min, or row in a terminal error state.
    error = "error"


class ClusterTunnel(
    UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base
):
    __tablename__ = "cluster_tunnels"
    __table_args__ = (
        # Two customers shouldn't ever share a tunnel IP on the mesh;
        # the coordinator allocates from 10.200.0.0/16 with this
        # constraint as the source of truth.
        UniqueConstraint("tunnel_ip", name="uq_cluster_tunnels_tunnel_ip"),
        # One cluster per (tenant, slug) — slug is what the UI uses
        # in URLs and what the customer types when running the edge.
        UniqueConstraint(
            "tenant_id", "slug", name="uq_cluster_tunnels_tenant_slug"
        ),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ClusterTunnelStatus] = mapped_column(
        SAEnum(ClusterTunnelStatus, name="cluster_tunnel_status"),
        default=ClusterTunnelStatus.pending,
        nullable=False,
    )

    # Operator-side WireGuard keypair (private key Fernet-encrypted at
    # rest with daalu_automation.core.crypto.encrypt_secret).
    operator_pubkey: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_privkey_encrypted: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    # Customer-side pubkey + endpoint reported via the bootstrap
    # callback (null until the edge first checks in).
    customer_pubkey: Mapped[str | None] = mapped_column(String(64))
    customer_endpoint: Mapped[str | None] = mapped_column(String(255))

    # Address on the 10.200.0.0/16 mesh. The kubeconfig stored in the
    # related Integration row references this IP as the API server.
    tunnel_ip: Mapped[str] = mapped_column(INET, nullable=False)

    # sha256 of a one-shot bearer token used by the customer-side edge
    # to authenticate the bootstrap callback. Cleared after first use
    # so a leaked token can only bootstrap once.
    invite_token_hash: Mapped[str | None] = mapped_column(String(64))

    last_handshake_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
