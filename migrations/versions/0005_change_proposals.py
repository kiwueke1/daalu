"""ChangeProposal table for the SoT / device-management pipeline.

Revision ID: 0005_change_proposals
Revises: 0004_alert_occurrences
Create Date: 2026-05-21

Adds the ``change_proposals`` table that backs the approval-gated push
pipeline: every device-config change — whether authored by the AI
engine, a human, or the drift reconciler — lands here as ``pending``
and only moves to ``approved`` after explicit human sign-off. The
executor service (a separate identity) is the only thing allowed to
flip a row to ``executed``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_change_proposals"
down_revision = "0004_alert_occurrences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Pre-create the ENUM types explicitly so the column declarations can
    # use ``create_type=False`` — mirrors the pattern in
    # 0002_alert_chat_actions.
    kind_enum = postgresql.ENUM(
        "intended_change",
        "drift",
        "manual",
        name="change_proposal_kind",
        create_type=False,
    )
    status_enum = postgresql.ENUM(
        "pending",
        "approved",
        "rejected",
        "executed",
        "failed",
        "stale",
        name="change_proposal_status",
        create_type=False,
    )
    kind_enum.create(op.get_bind(), checkfirst=True)
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "change_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("kind", kind_enum, nullable=False),
        sa.Column(
            "status",
            status_enum,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("intended_config", sa.Text(), nullable=False, server_default=""),
        sa.Column("observed_config", sa.Text(), nullable=False, server_default=""),
        sa.Column("diff", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "renderer_version",
            sa.String(length=32),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "approved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "executor_result",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_change_proposals_tenant_id",
        "change_proposals",
        ["tenant_id"],
    )
    op.create_index(
        "ix_change_proposals_device_id",
        "change_proposals",
        ["device_id"],
    )
    op.create_index(
        "ix_change_proposals_tenant_status",
        "change_proposals",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_change_proposals_tenant_device_status",
        "change_proposals",
        ["tenant_id", "device_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_change_proposals_tenant_device_status",
        table_name="change_proposals",
    )
    op.drop_index(
        "ix_change_proposals_tenant_status", table_name="change_proposals"
    )
    op.drop_index(
        "ix_change_proposals_device_id", table_name="change_proposals"
    )
    op.drop_index(
        "ix_change_proposals_tenant_id", table_name="change_proposals"
    )
    op.drop_table("change_proposals")
    bind = op.get_bind()
    postgresql.ENUM(name="change_proposal_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="change_proposal_kind").drop(bind, checkfirst=True)
