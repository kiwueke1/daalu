"""Integration ↔ ClusterTunnel link for cross-cluster routing.

A NULL ``cluster_tunnel_id`` keeps today's behavior — the integration's
URL is dialed directly from the hub. When set, the integration's URL is
dialed through the named tunnel's edge proxy, so a customer can paste an
in-cluster ``*.svc.cluster.local`` URL and have it resolved inside the
workload cluster.

The column is intentionally on every integration row, not just
observability — kubernetes/cloud rows may use it later. ON DELETE
SET NULL so removing a cluster doesn't cascade-wipe integration rows
the operator can repoint at a new cluster.

Revision ID: 0016_integration_cluster_tunnel
Revises: 0015_report_schedules
Create Date: 2026-05-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_integration_cluster_tunnel"
down_revision = "0015_report_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integrations",
        sa.Column(
            "cluster_tunnel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cluster_tunnels.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_integrations_cluster_tunnel_id",
        "integrations",
        ["cluster_tunnel_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_integrations_cluster_tunnel_id", table_name="integrations")
    op.drop_column("integrations", "cluster_tunnel_id")
