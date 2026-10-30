"""gpu_tenants — per-tenant local-GPU (vLLM) lifecycle.

One row per Daalu tenant that onboarded their GPU. The gpu-controller
reconciles these rows into the existing ``deploy/k8s/gpu`` vLLM stack,
either in the operator's cluster or — when ``target_cluster_tunnel_id``
is set — in the customer's own cluster reached via WireGuard.

This is the operator-side materialisation state (mirrors
``nautobot_tenants``). The customer-facing surface is the tenant's
SOVEREIGN routing config on ``tenants`` (written by the provision route
once a row is ``active``).

Revision ID: 0020_gpu_tenants
Revises: 0019_config_manager_tenants
Create Date: 2026-06-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0020_gpu_tenants"
down_revision = "0019_config_manager_tenants"
branch_labels = None
depends_on = None


_STATE_VALUES = (
    "pending",
    "provisioning",
    "active",
    "error",
    "deleting",
    "destroyed",
)


def upgrade() -> None:
    op.create_table(
        "gpu_tenants",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(*_STATE_VALUES, name="gpu_tenant_state", native_enum=True),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "target_cluster_tunnel_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("cluster_tunnels.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "namespace",
            sa.String(253),
            nullable=False,
            server_default="daalu",
        ),
        sa.Column(
            "gpu_class",
            sa.String(64),
            nullable=False,
            server_default="ada-16",
        ),
        sa.Column("gpu_node", sa.String(253), nullable=True),
        sa.Column(
            "model_classifier",
            sa.String(255),
            nullable=False,
            server_default="meta/llama-3.1-8b-instruct",
        ),
        sa.Column("model_quality", sa.String(255), nullable=True),
        sa.Column("hf_token_ciphertext", sa.Text, nullable=True),
        sa.Column("service_url", sa.String(512), nullable=True),
        sa.Column("last_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("tenant_id", name="uq_gpu_tenants_tenant_id"),
    )
    op.create_index("ix_gpu_tenants_state", "gpu_tenants", ["state"])


def downgrade() -> None:
    op.drop_index("ix_gpu_tenants_state", table_name="gpu_tenants")
    op.drop_table("gpu_tenants")
    sa.Enum(name="gpu_tenant_state").drop(op.get_bind(), checkfirst=True)
