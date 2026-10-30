"""Add ``model`` (coding-model catalog id) to workspaces.

A workspace user picks a coding model in the Create-Workspace UI; we
persist the catalog id so the reconciler can inject the right
``DAALU_MODEL`` into the code-server pod. Nullable — existing rows
predate the column and fall back to the catalog default.

No CHECK constraint: the set of valid model ids lives in
``core.model_catalog`` and evolves in code, unlike the stable
state/profile enums.

Revision ID: 0025_workspace_model
Revises: 0024_aiperf_runs
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_workspace_model"
down_revision = "0024_aiperf_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("model", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "model")
