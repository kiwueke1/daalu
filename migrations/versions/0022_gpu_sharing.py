"""GPU sharing — provider grant, shared flag, pool registry, revenue ledger.

Adds the multi-tenant GPU-sharing model (one tenant's card served to many,
billed to the consumer, credited to the owner):

* ``tenants.is_gpu_provider`` — superuser grant; only a provider may share.
* ``gpu_tenants.shared`` — marks a stack as a shared pool.
* ``gpu_pools`` — the upstreams the inference-gateway may serve from.
* ``gpu_revenue_shares`` — per-call credit to the GPU owner.

The "shared ⇒ provider" invariant is enforced by a BEFORE INSERT/UPDATE
trigger on ``gpu_tenants`` (a Postgres CHECK constraint cannot contain the
cross-table subquery), in addition to the application-layer guard in the
gpu-controller. See
docs/plans/nvidia-ai-factory/13-gpu-sharing-and-multi-tenant-marketplace.md.

Revision ID: 0022_gpu_sharing
Revises: 0021_tenant_sovereign_inference
Create Date: 2026-06-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0022_gpu_sharing"
down_revision = "0021_tenant_sovereign_inference"
branch_labels = None
depends_on = None


_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION gpu_tenants_shared_requires_provider()
RETURNS trigger AS $$
BEGIN
    IF NEW.shared AND NOT EXISTS (
        SELECT 1 FROM tenants
        WHERE tenants.id = NEW.tenant_id AND tenants.is_gpu_provider
    ) THEN
        RAISE EXCEPTION
            'gpu_tenants.shared requires tenant % to hold is_gpu_provider',
            NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGER = """
CREATE TRIGGER trg_gpu_tenants_shared_requires_provider
BEFORE INSERT OR UPDATE ON gpu_tenants
FOR EACH ROW EXECUTE FUNCTION gpu_tenants_shared_requires_provider();
"""


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "is_gpu_provider",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "gpu_tenants",
        sa.Column(
            "shared",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.create_table(
        "gpu_pools",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "provider_tenant_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "gpu_tenant_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("gpu_tenants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("upstream_url", sa.String(512), nullable=False),
        sa.Column(
            "served_models", JSONB, nullable=False, server_default="[]"
        ),
        sa.Column(
            "gpu_class", sa.String(64), nullable=False, server_default="ada-48"
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("capacity_hint", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "gpu_revenue_shares",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "consumer_tenant_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "provider_tenant_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "gpu_pool_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("gpu_pools.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "completion_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("gross_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column(
            "platform_take_rate", sa.Numeric(5, 4), nullable=False, server_default="0"
        ),
        sa.Column(
            "provider_credit_usd", sa.Numeric(12, 6), nullable=False, server_default="0"
        ),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, index=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.execute(_TRIGGER_FN)
    op.execute(_TRIGGER)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_gpu_tenants_shared_requires_provider "
        "ON gpu_tenants"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS gpu_tenants_shared_requires_provider()"
    )
    op.drop_table("gpu_revenue_shares")
    op.drop_table("gpu_pools")
    op.drop_column("gpu_tenants", "shared")
    op.drop_column("tenants", "is_gpu_provider")
