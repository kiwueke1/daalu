"""Daalu-hosted GPU quotas, sovereign-tier per-tenant config, and workspaces.

Revision ID: 0008_daalu_hosted_and_workspaces
Revises: 0007_user_settings_pats_feedback
Create Date: 2026-05-25

Three additions:

* ``tenants.feature_flags`` — JSONB for per-tenant toggles. Reads
  cache-friendly across the request hot path; writes are admin-only.
* ``tenants.sovereign_inference_url`` /
  ``tenants.sovereign_inference_token_hash`` —
  per-tenant config for the SOVEREIGN tier (the customer's own GPU,
  reached through the federation tunnel). The token is sha256-hashed,
  same pattern as ingest_api_key_hash.
* ``daalu_hosted_quotas`` — per-tenant monthly token quota for the
  daalu-hosted (operator-owned) inference pool. Missing row = tier
  not enabled.
* ``workspaces`` — one row per (tenant, user) tracking the lifecycle
  state of their code-server workspace pod. Drives the
  workspace-controller's reconcile loop.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_daalu_hosted_and_workspaces"
down_revision = "0007_user_settings_pats_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── tenants: feature_flags JSONB + sovereign config ─────────────
    op.add_column(
        "tenants",
        sa.Column(
            "feature_flags",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "tenants",
        sa.Column("sovereign_inference_url", sa.String(512), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("sovereign_inference_token_hash", sa.String(64), nullable=True),
    )

    # ── daalu_hosted_quotas ─────────────────────────────────────────
    op.create_table(
        "daalu_hosted_quotas",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "monthly_token_limit",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "current_period_used",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "period_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "overage_policy",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'throttle'"),
        ),
        sa.Column(
            "rate_limit_rpm",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
        sa.Column(
            "rate_limit_tpm",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("50000"),
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
            "overage_policy IN ('hard_stop','throttle','cloud_overflow')",
            name="ck_daalu_hosted_quotas_overage_policy",
        ),
    )

    # ── workspaces ──────────────────────────────────────────────────
    op.create_table(
        "workspaces",
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
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "state",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'provisioning'"),
        ),
        sa.Column(
            "profile",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'small'"),
        ),
        sa.Column("pod_name", sa.String(255), nullable=True),
        sa.Column("pvc_name", sa.String(255), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("git_repo_url", sa.String(512), nullable=True),
        sa.Column(
            "git_branch", sa.String(255), nullable=False, server_default=sa.text("'main'")
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
        sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_workspaces_tenant_user"),
        sa.CheckConstraint(
            "state IN ('provisioning','active','paused','destroyed','error')",
            name="ck_workspaces_state",
        ),
        sa.CheckConstraint(
            "profile IN ('small','medium','large')",
            name="ck_workspaces_profile",
        ),
    )


def downgrade() -> None:
    op.drop_table("workspaces")
    op.drop_table("daalu_hosted_quotas")
    op.drop_column("tenants", "sovereign_inference_token_hash")
    op.drop_column("tenants", "sovereign_inference_url")
    op.drop_column("tenants", "feature_flags")
