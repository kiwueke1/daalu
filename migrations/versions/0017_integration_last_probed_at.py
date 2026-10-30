"""Integration.last_probed_at — health-check timestamps.

Adds a single column so the new ``integrations.health_check`` beat task
can record the most recent probe. Combined with the existing
``status`` + ``last_error`` columns, the UI can render "Prometheus —
error, last probed 47 s ago: connection refused" without any extra
state.

The column is nullable because every row created before this migration
has never been probed. The first beat tick after deploy will populate
it for every row.

Revision ID: 0017_integration_last_probed_at
Revises: 0016_integration_cluster_tunnel
Create Date: 2026-05-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_integration_last_probed_at"
down_revision = "0016_integration_cluster_tunnel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integrations",
        sa.Column(
            "last_probed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("integrations", "last_probed_at")
