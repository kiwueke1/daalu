"""config_manager_tenants + provision_op proposal kind.

Adds the operator-side materialisation table for per-tenant NVIDIA Config
Manager (NV-CM) stacks (network config-management plane), mirroring
``nautobot_tenants``. Also extends the ``change_proposal_kind`` enum with
``provision_op`` — the imperative server-lifecycle proposal kind executed
via Tinkerbell (observed-state-compared by the gate rather than
render-drift-checked).

See docs/design/nv-config-manager-integration.md §10, §18.

Revision ID: 0019_config_manager_tenants
Revises: 0018_nautobot_tenants
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0019_config_manager_tenants"
down_revision = "0018_nautobot_tenants"
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
        "config_manager_tenants",
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
            sa.Enum(
                *_STATE_VALUES,
                name="config_manager_tenant_state",
                native_enum=True,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "target_cluster_tunnel_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("cluster_tunnels.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("namespace", sa.String(253), nullable=False),
        sa.Column("base_hostname", sa.String(253), nullable=False),
        sa.Column(
            "components",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "size_profile",
            sa.String(16),
            nullable=False,
            server_default="small",
        ),
        sa.Column(
            "chart_version", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column(
            "urls", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("keycloak_client_id", sa.String(255), nullable=True),
        sa.Column(
            "keycloak_client_secret_ciphertext", sa.Text, nullable=True
        ),
        sa.Column("secrets_ciphertext", sa.Text, nullable=True),
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
        sa.UniqueConstraint(
            "tenant_id", name="uq_config_manager_tenants_tenant_id"
        ),
    )
    op.create_index(
        "ix_config_manager_tenants_state",
        "config_manager_tenants",
        ["state"],
    )

    # Extend the existing change_proposal_kind enum. ADD VALUE IF NOT
    # EXISTS is allowed inside a transaction on PG 12+; the new value is
    # only *used* by later code, never in this migration, so it commits
    # cleanly. Enum-value removal isn't supported by Postgres, so the
    # downgrade leaves it in place (harmless).
    op.execute(
        "ALTER TYPE change_proposal_kind ADD VALUE IF NOT EXISTS 'provision_op'"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_config_manager_tenants_state",
        table_name="config_manager_tenants",
    )
    op.drop_table("config_manager_tenants")
    sa.Enum(name="config_manager_tenant_state").drop(
        op.get_bind(), checkfirst=True
    )
    # 'provision_op' enum value intentionally left in change_proposal_kind:
    # Postgres cannot drop enum values without recreating the type.
