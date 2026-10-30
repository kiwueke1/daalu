"""WireGuard tunnel coordination — one row per onboarded cluster.

Revision ID: 0003_cluster_tunnels
Revises: 0002_alert_chat_actions
Create Date: 2026-05-17

Adds ``cluster_tunnels`` for tracking the operator-side WireGuard
peer state for each customer cluster: keypair, allocated tunnel IP
on 10.200.0.0/16, customer-reported pubkey, invite token hash, and
the last_handshake_at heartbeat the tunnel-health worker refreshes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_cluster_tunnels"
down_revision = "0002_alert_chat_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tunnel_status = postgresql.ENUM(
        "pending",
        "awaiting_handshake",
        "connected",
        "degraded",
        "error",
        name="cluster_tunnel_status",
        create_type=False,
    )
    tunnel_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "cluster_tunnels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", tunnel_status, nullable=False, server_default="pending"),
        sa.Column("operator_pubkey", sa.String(64), nullable=False),
        sa.Column("operator_privkey_encrypted", sa.Text(), nullable=False),
        sa.Column("customer_pubkey", sa.String(64), nullable=True),
        sa.Column("customer_endpoint", sa.String(255), nullable=True),
        sa.Column("tunnel_ip", postgresql.INET(), nullable=False),
        sa.Column("invite_token_hash", sa.String(64), nullable=True),
        sa.Column(
            "last_handshake_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("tunnel_ip", name="uq_cluster_tunnels_tunnel_ip"),
        sa.UniqueConstraint(
            "tenant_id", "slug", name="uq_cluster_tunnels_tenant_slug"
        ),
    )


def downgrade() -> None:
    op.drop_table("cluster_tunnels")
    sa.Enum(name="cluster_tunnel_status").drop(op.get_bind(), checkfirst=True)
