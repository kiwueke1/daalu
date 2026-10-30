"""Multi-tenancy: ingest key per tenant, soft-delete, platform superuser.

Revision ID: 0001_multi_tenancy
Revises:
Create Date: 2026-05-15

Adds the columns needed to flip the platform from Phase-1 single-tenant
(one INGEST_API_KEY env var, one operator install per customer) to
Phase-2 multi-tenant (N tenants per install, per-tenant ingest keys
looked up by sha256 hash, platform superusers managing them via the
API).

Existing rows pointing at DEFAULT_TENANT_ID are unaffected — the bootstrap
hook in core.bootstrap seeds the default tenant's ingest_api_key_hash
from the env var on next startup, so single-tenant deployments keep
working without an operator-side rotation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_multi_tenancy"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("ingest_api_key_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_tenants_ingest_api_key_hash",
        "tenants",
        ["ingest_api_key_hash"],
        unique=True,
    )
    op.add_column(
        "tenants",
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "is_superuser",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "is_superuser")
    op.drop_column("tenants", "is_deleted")
    op.drop_index("ix_tenants_ingest_api_key_hash", table_name="tenants")
    op.drop_column("tenants", "ingest_api_key_hash")
