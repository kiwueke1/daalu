"""AIPerf load-test / benchmark runs (AI-factory UI, super-admin only).

A row per AIPerf concurrency sweep kicked from the hub. daalu-api writes it
``pending``; the gpu-controller runs the AIPerf Job on the operator cluster and
writes back ``summary`` (TTFT/ITL/throughput per concurrency) + ``output``.

Revision ID: 0024_aiperf_runs
Revises: 0023_gpu_diagnostic_runs
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0024_aiperf_runs"
down_revision = "0023_gpu_diagnostic_runs"
branch_labels = None
depends_on = None

# Let op.create_table own the enum lifecycle (default create_type=True): it
# emits CREATE TYPE once then CREATE TABLE in one transaction. Do NOT also call
# _STATE.create() explicitly — create_table re-emits CREATE TYPE without
# checkfirst and the two collide ("type already exists"). downgrade()'s
# drop_table drops the type symmetrically.
_STATE = sa.Enum(
    "pending", "running", "passed", "failed", "error", name="aiperf_run_state"
)


def upgrade() -> None:
    op.create_table(
        "aiperf_runs",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("state", _STATE, nullable=False, server_default="pending"),
        sa.Column("target_url", sa.String(512), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column(
            "endpoint_type", sa.String(32), nullable=False, server_default="chat"
        ),
        sa.Column("concurrency", sa.String(255), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "streaming", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "via_gateway", sa.Boolean(), nullable=False, server_default=sa.false()
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
    op.drop_table("aiperf_runs")
    bind = op.get_bind()
    _STATE.drop(bind, checkfirst=True)
