"""Link workflow runs to alerts + store per-step detail.

Remediation runs (created when an operator clicks "Approve & run" on an
alert) are recorded as ``workflow_runs`` rows so the Workflows page can list
them and a detail page can replay each step. Two new columns:

- ``alert_id`` — the alert the run remediates (nullable; code-registered
  automations leave it null), so the run links back to its source alert.
- ``steps`` — an ordered JSON list of the tool calls the agent executed
  (tool, input, output, outcome) for the detail view.

Revision ID: 0029_workflow_run_alert_steps
Revises: 0028_cli_device_auth
Create Date: 2026-06-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0029_workflow_run_alert_steps"
down_revision = "0028_cli_device_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("alert_id", PG_UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_workflow_runs_alert_id", "workflow_runs", ["alert_id"]
    )
    op.create_foreign_key(
        "fk_workflow_runs_alert_id",
        "workflow_runs",
        "alerts",
        ["alert_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "workflow_runs",
        sa.Column(
            "steps",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workflow_runs", "steps")
    op.drop_constraint(
        "fk_workflow_runs_alert_id", "workflow_runs", type_="foreignkey"
    )
    op.drop_index("ix_workflow_runs_alert_id", table_name="workflow_runs")
    op.drop_column("workflow_runs", "alert_id")
