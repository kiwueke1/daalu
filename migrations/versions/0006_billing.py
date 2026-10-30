"""Billing tables — SKU catalog, tenant subscription, usage events.

Revision ID: 0006_billing
Revises: 0005_change_proposals
Create Date: 2026-05-22

Adds the three tables that power per-tenant LLM-call billing:

* ``skus`` — catalog of plans (Local-First, Hybrid, External-Only,
  Sovereign). Owned by the operator, not scoped to a tenant.
* ``tenant_skus`` — per-tenant subscription history. ``current=True``
  marks the active row.
* ``usage_events`` — append-only LLM-call log. ``cost_usd`` is computed
  at write time against the SKU's per-tier rates so historical rows
  survive future price changes.

Indexes are sized for the two queries that matter:
  * current period totals: ``WHERE tenant_id = ? AND occurred_at > ?``
  * by-source breakdown:  ``WHERE tenant_id = ? GROUP BY source``
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_billing"
down_revision = "0005_change_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    routing_enum = postgresql.ENUM(
        "local_first",
        "hybrid",
        "external_only",
        "sovereign",
        name="routing_policy",
        create_type=False,
    )
    tier_enum = postgresql.ENUM(
        "local",
        "external_classifier",
        "external_quality",
        "sovereign",
        name="inference_tier",
        create_type=False,
    )
    routing_enum.create(op.get_bind(), checkfirst=True)
    tier_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "skus",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False, unique=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("tagline", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "routing_policy",
            routing_enum,
            nullable=False,
            server_default="local_first",
        ),
        sa.Column(
            "monthly_base_usd",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "included_events_per_month",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("price_local_in_per_mtok", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("price_local_out_per_mtok", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("price_external_classifier_in_per_mtok", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("price_external_classifier_out_per_mtok", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("price_external_quality_in_per_mtok", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("price_external_quality_out_per_mtok", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("monthly_soft_cap_usd", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_skus_slug", "skus", ["slug"], unique=True)

    op.create_table(
        "tenant_skus",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sku_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("skus.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tenant_skus_tenant_id", "tenant_skus", ["tenant_id"])
    op.create_index("ix_tenant_skus_sku_id", "tenant_skus", ["sku_id"])
    op.create_index("ix_tenant_skus_current", "tenant_skus", ["current"])
    # Only one current row per tenant.
    op.create_index(
        "ux_tenant_skus_one_current_per_tenant",
        "tenant_skus",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("current = true"),
    )

    op.create_table(
        "usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tier", tier_enum, nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column(
            "sku_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("skus.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_events_tenant_id", "usage_events", ["tenant_id"])
    op.create_index("ix_usage_events_tier", "usage_events", ["tier"])
    op.create_index("ix_usage_events_source", "usage_events", ["source"])
    op.create_index("ix_usage_events_sku_id", "usage_events", ["sku_id"])
    op.create_index("ix_usage_events_occurred_at", "usage_events", ["occurred_at"])
    op.create_index(
        "ix_usage_events_tenant_occurred",
        "usage_events",
        ["tenant_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_events_tenant_occurred", table_name="usage_events")
    op.drop_index("ix_usage_events_occurred_at", table_name="usage_events")
    op.drop_index("ix_usage_events_sku_id", table_name="usage_events")
    op.drop_index("ix_usage_events_source", table_name="usage_events")
    op.drop_index("ix_usage_events_tier", table_name="usage_events")
    op.drop_index("ix_usage_events_tenant_id", table_name="usage_events")
    op.drop_table("usage_events")

    op.drop_index("ux_tenant_skus_one_current_per_tenant", table_name="tenant_skus")
    op.drop_index("ix_tenant_skus_current", table_name="tenant_skus")
    op.drop_index("ix_tenant_skus_sku_id", table_name="tenant_skus")
    op.drop_index("ix_tenant_skus_tenant_id", table_name="tenant_skus")
    op.drop_table("tenant_skus")

    op.drop_index("ix_skus_slug", table_name="skus")
    op.drop_table("skus")

    bind = op.get_bind()
    postgresql.ENUM(name="inference_tier").drop(bind, checkfirst=True)
    postgresql.ENUM(name="routing_policy").drop(bind, checkfirst=True)
