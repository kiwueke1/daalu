"""nautobot_tenants — per-tenant hosted Nautobot lifecycle.

One row per Daalu tenant that opts into the hosted-Nautobot tier.
Replaces the pre-existing "shared-Nautobot + ObjectPermission slice"
provisioning model: each tenant now gets a fully isolated Nautobot
stack (web + worker + scheduler + postgres + redis), either in the
operator's cluster or — when ``target_cluster_tunnel_id`` is set — in
the customer's own cluster reached via WireGuard.

The previous model didn't have rows of its own; the only artefact was
the ``Integration(provider="nautobot")`` row. That row stays as the
customer-facing surface (URL + token); this new table is the
operator-side materialisation state.

Revision ID: 0018_nautobot_tenants
Revises: 0017_integration_last_probed_at
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0018_nautobot_tenants"
down_revision = "0017_integration_last_probed_at"
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
    # Let op.create_table create the ENUM type via the column's default
    # native-enum auto-create. A previous version called
    # state_enum.create(checkfirst=True) explicitly *and* let
    # create_table create it again — same transaction, double CREATE TYPE,
    # second one duplicate-objects out. Reusing the same Enum name on a
    # second instance with create_type=False didn't help (SQLAlchemy's
    # native-enum metadata keyed by name silently ignores the override).
    op.create_table(
        "nautobot_tenants",
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
                name="nautobot_tenant_state",
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
        sa.Column("hostname", sa.String(253), nullable=True),
        sa.Column("admin_token_ciphertext", sa.Text, nullable=True),
        sa.Column("postgres_password_ciphertext", sa.Text, nullable=True),
        sa.Column("secret_key_ciphertext", sa.Text, nullable=True),
        sa.Column(
            "last_ready_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
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
        sa.UniqueConstraint("tenant_id", name="uq_nautobot_tenants_tenant_id"),
    )
    op.create_index(
        "ix_nautobot_tenants_state",
        "nautobot_tenants",
        ["state"],
    )


def downgrade() -> None:
    op.drop_index("ix_nautobot_tenants_state", table_name="nautobot_tenants")
    op.drop_table("nautobot_tenants")
    sa.Enum(name="nautobot_tenant_state").drop(op.get_bind(), checkfirst=True)
