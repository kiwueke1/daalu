"""Report schedules — cron-driven delivery of saved reports.

Revision ID: 0015_report_schedules
Revises: 0014_dashboards
Create Date: 2026-05-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_report_schedules"
down_revision = "0014_dashboards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_schedules",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "saved_report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("saved_reports.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("cron", sa.String(64), nullable=False),
        sa.Column("destination", sa.String(16), nullable=False),
        sa.Column(
            "recipient",
            sa.String(255),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "format",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'markdown'"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), index=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "destination IN ('slack','email')",
            name="ck_report_schedules_destination",
        ),
        sa.CheckConstraint(
            "format IN ('markdown','csv')",
            name="ck_report_schedules_format",
        ),
    )


def downgrade() -> None:
    op.drop_table("report_schedules")
