"""AI-Ops Maturity Model tables + alert_actions policy columns.

Adds the tables behind docs/design/ai-ops-maturity.md: autonomy policies,
per-tenant kill switch / breaker state, investigations, the hash-chained
audit log, runbooks, eval runs and maturity self-assessments. Extends
``alert_actions`` with the policy decision, multi-approver approvals,
preview and verification outcome.

Additive only. Existing ``alert_actions`` rows get ``policy_mode=''``,
``approvals_required=1`` and ``actor='agent'`` — the pre-AOMM behaviour.

Revision ID: 0034_aiops_maturity
Revises: 0033_gpu_tenant_mem_util
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034_aiops_maturity"
down_revision = "0033_gpu_tenant_mem_util"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)


def _base_cols() -> list[sa.Column]:
    return [
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "tenant_id", _UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "autonomy_policies",
        *_base_cols(),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("yaml", sa.Text(), nullable=False),
        sa.Column("note", sa.String(512), nullable=False, server_default=""),
        sa.Column("updated_by", _UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.UniqueConstraint("tenant_id", "version", name="uq_autonomy_policy_version"),
    )

    op.create_table(
        "aiops_tenant_state",
        sa.Column(
            "tenant_id", _UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kill_switch", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("kill_switch_by", sa.String(255), nullable=False, server_default=""),
        sa.Column("kill_switch_at", sa.DateTime(timezone=True)),
        sa.Column("breaker", sa.JSON(), nullable=False, server_default="{}"),
    )

    op.create_table(
        "investigations",
        *_base_cols(),
        sa.Column(
            "alert_id", _UUID, sa.ForeignKey("alerts.id", ondelete="CASCADE"),
            nullable=False, unique=True, index=True,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("diagnosed_at", sa.DateTime(timezone=True)),
        sa.Column("root_cause", sa.Text(), nullable=False, server_default=""),
        sa.Column("actions_proposed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actions_approved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actions_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actions_auto", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolution", sa.String(16), nullable=False, server_default="open", index=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("verified", sa.Boolean()),
        sa.Column("runbook_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("verdict", sa.String(16), index=True),
        sa.Column("verdict_by", sa.String(255), nullable=False, server_default=""),
        sa.Column("verdict_at", sa.DateTime(timezone=True)),
        sa.Column("verdict_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
    )

    op.create_table(
        "audit_events",
        *_base_cols(),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False, index=True),
        sa.Column("actor", sa.String(255), nullable=False, server_default=""),
        sa.Column("subject", sa.String(255), nullable=False, server_default="", index=True),
        sa.Column("data", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("prev_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("tenant_id", "seq", name="uq_audit_event_seq"),
    )

    op.create_table(
        "runbooks",
        *_base_cols(),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("match", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("allowed_actions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft", index=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "from_investigation_id", _UUID,
            sa.ForeignKey("investigations.id", ondelete="SET NULL"),
        ),
        sa.Column("published_by", sa.String(255), nullable=False, server_default=""),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_runbook_slug"),
    )

    op.create_table(
        "eval_runs",
        *_base_cols(),
        sa.Column("scenario_id", sa.String(128), nullable=False, index=True),
        sa.Column("label", sa.String(128), nullable=False, server_default=""),
        sa.Column("alert_id", _UUID, sa.ForeignKey("alerts.id", ondelete="SET NULL")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
    )

    op.create_table(
        "maturity_assessments",
        *_base_cols(),
        sa.Column("answers", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(255), nullable=False, server_default=""),
    )

    with op.batch_alter_table("alert_actions") as b:
        b.add_column(sa.Column("policy_mode", sa.String(16), nullable=False, server_default=""))
        b.add_column(sa.Column("policy_rule", sa.String(128), nullable=False, server_default=""))
        b.add_column(sa.Column("policy_reason", sa.Text(), nullable=False, server_default=""))
        b.add_column(sa.Column("approvals_required", sa.Integer(), nullable=False, server_default="1"))
        b.add_column(sa.Column("approvals", sa.JSON(), nullable=False, server_default="[]"))
        b.add_column(sa.Column("preview", sa.JSON(), nullable=False, server_default="{}"))
        b.add_column(sa.Column("auto_executed", sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("verified_resolved", sa.Boolean()))
        b.add_column(sa.Column("actor", sa.String(255), nullable=False, server_default="agent"))


def downgrade() -> None:
    with op.batch_alter_table("alert_actions") as b:
        for col in (
            "actor", "verified_resolved", "auto_executed", "preview", "approvals",
            "approvals_required", "policy_reason", "policy_rule", "policy_mode",
        ):
            b.drop_column(col)
    for table in (
        "maturity_assessments", "eval_runs", "runbooks", "audit_events",
        "investigations", "aiops_tenant_state", "autonomy_policies",
    ):
        op.drop_table(table)
