"""Per-alert chat + remediation action tables.

Revision ID: 0002_alert_chat_actions
Revises: 0001_multi_tenancy
Create Date: 2026-05-16

Adds the storage backing the per-alert chat panel: a chronological
message log (``alert_chat_messages``) and a ledger of proposed /
executed tool calls (``alert_actions``). Write actions land as pending
rows until the operator clicks Approve in the UI.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_alert_chat_actions"
down_revision = "0001_multi_tenancy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the ENUM types explicitly so we can mark them
    # ``create_type=False`` on the columns below — otherwise the implicit
    # CREATE TYPE inside ``op.create_table`` would race the explicit one.
    chat_role = postgresql.ENUM(
        "user", "assistant", "tool", name="alert_chat_role", create_type=False
    )
    action_status = postgresql.ENUM(
        "pending",
        "approved",
        "rejected",
        "executed",
        "failed",
        name="alert_action_status",
        create_type=False,
    )
    chat_role.create(op.get_bind(), checkfirst=True)
    action_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "alert_chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "alert_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("alerts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", chat_role, nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "tool_calls_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_alert_chat_messages_alert_id",
        "alert_chat_messages",
        ["alert_id"],
    )
    op.create_index(
        "ix_alert_chat_messages_tenant_id",
        "alert_chat_messages",
        ["tenant_id"],
    )

    op.create_table(
        "alert_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "alert_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("alerts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("alert_chat_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_call_id", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column(
            "tool_input",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "requires_approval",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "status",
            action_status,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("result_output", sa.Text(), nullable=False, server_default=""),
        sa.Column("result_error", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "approved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_alert_actions_alert_id", "alert_actions", ["alert_id"])
    op.create_index("ix_alert_actions_message_id", "alert_actions", ["message_id"])
    op.create_index("ix_alert_actions_status", "alert_actions", ["status"])
    op.create_index("ix_alert_actions_tenant_id", "alert_actions", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("alert_actions")
    op.drop_table("alert_chat_messages")
    postgresql.ENUM(name="alert_action_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="alert_chat_role").drop(op.get_bind(), checkfirst=True)
