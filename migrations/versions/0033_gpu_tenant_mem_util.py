"""Per-tenant vLLM GPU memory fraction.

The gpu-controller re-applies the classifier Deployment every tick with a
hard-coded ``--gpu-memory-utilization=0.90``, so a card can never be shared
with a second vLLM (time-slicing): a hand edit is reverted within 30 s. This
column lets a row carry its own fraction.

Additive and nullable. NULL keeps the old meaning — the model's catalog
default, which for every existing row is the 0.90 it already runs at.

Revision ID: 0033_gpu_tenant_mem_util
Revises: 0032_change_proposal_environment
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_gpu_tenant_mem_util"
down_revision = "0032_change_proposal_environment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "gpu_tenants",
        sa.Column("gpu_memory_utilization", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("gpu_tenants", "gpu_memory_utilization")
