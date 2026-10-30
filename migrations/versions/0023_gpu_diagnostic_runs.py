"""GPU diagnostic / validation runs (AI-factory UI).

dcgmi diag / nccl-tests / observability-validate runs triggered from the hub UI.

Revision ID: 0023_gpu_diagnostic_runs
Revises: 0022_gpu_sharing
Create Date: 2026-06-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0023_gpu_diagnostic_runs"
down_revision = "0022_gpu_sharing"
branch_labels = None
depends_on = None

# Let op.create_table own the enum lifecycle (default create_type=True): it
# emits CREATE TYPE for each enum once, then CREATE TABLE, all in one
# transaction. Do NOT also call _KIND.create()/_STATE.create() explicitly —
# create_table re-emits CREATE TYPE without checkfirst, so the two collide with
# "type ... already exists" (the original bug). downgrade()'s drop_table drops
# the types symmetrically.
_KIND = sa.Enum(
    "dcgmi_diag", "nccl_test", "observability_validate", name="gpu_diagnostic_kind"
)
_STATE = sa.Enum(
    "pending", "running", "passed", "failed", "error", name="gpu_diagnostic_state"
)


def upgrade() -> None:
    op.create_table(
        "gpu_diagnostic_runs",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
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
        sa.Column("kind", _KIND, nullable=False),
        sa.Column("level", sa.Integer(), nullable=True),
        sa.Column(
            "state", _STATE, nullable=False, server_default="pending"
        ),
        sa.Column("summary", JSONB, nullable=False, server_default="{}"),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("gpu_diagnostic_runs")
    bind = op.get_bind()
    _STATE.drop(bind, checkfirst=True)
    _KIND.drop(bind, checkfirst=True)
