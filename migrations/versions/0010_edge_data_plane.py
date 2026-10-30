"""Tenant edge-data URL + token columns.

Revision ID: 0010_edge_data_plane
Revises: 0009_daalu_private
Create Date: 2026-05-25

The hub forwarder reads ``tenants.edge_data_url`` to decide where
to send tenant-scoped requests for Private tenants. The cleartext
service token mounted on the edge pod is sha256-hashed for storage
on the hub (same pattern as ingest_api_key_hash and sovereign_
inference_token_hash).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_edge_data_plane"
down_revision = "0009_daalu_private"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("edge_data_url", sa.Text(), nullable=True))
    op.add_column(
        "tenants",
        sa.Column("edge_data_token_hash", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "edge_data_token_hash")
    op.drop_column("tenants", "edge_data_url")
