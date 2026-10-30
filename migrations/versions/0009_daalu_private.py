"""Daalu Private tier flags + per-tenant DB URL.

Revision ID: 0009_daalu_private
Revises: 0008_daalu_hosted_and_workspaces
Create Date: 2026-05-25

Three new columns on ``tenants``:

* ``is_private`` — top-level toggle. Most consumers branch on
  this. The other two columns are only consulted when this is
  ``true``.
* ``edge_agents_enabled`` — if true, the agent host on the hub
  skips this tenant, and the customer's ``daalu-edge-agents`` pod
  picks it up.
* ``private_db_url`` / ``private_db_token_hash`` — when set, the
  tenant-DB router resolves tenant-scoped reads/writes to this URL
  instead of the hub DB. Token hash for the same reason as
  ingest_api_key_hash: cleartext never lands in the DB.

See ``docs/design/daalu-private.md`` for the architecture.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_daalu_private"
down_revision = "0008_daalu_hosted_and_workspaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "edge_agents_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("tenants", sa.Column("private_db_url", sa.Text(), nullable=True))
    op.add_column(
        "tenants",
        sa.Column("private_db_token_hash", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "private_db_token_hash")
    op.drop_column("tenants", "private_db_url")
    op.drop_column("tenants", "edge_agents_enabled")
    op.drop_column("tenants", "is_private")
