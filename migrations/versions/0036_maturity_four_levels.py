"""Collapse the AI-Ops maturity ladder from 0-5 to four levels (0 = not started).

Old Level 4 (Learning) folds into Trusted, so new Level 3 needs the old L3 and
L4 questions. A saved self-assessment of old 3 had L3 but not L4 → new 2;
old 4 → 3; old 5 → 4.

Revision ID: 0036_maturity_four_levels
Revises: 0035_trace_metric_names
"""

from __future__ import annotations

from alembic import op

revision = "0036_maturity_four_levels"
down_revision = "0035_trace_metric_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE maturity_assessments SET level = CASE level "
        "WHEN 3 THEN 2 WHEN 4 THEN 3 WHEN 5 THEN 4 ELSE level END"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE maturity_assessments SET level = CASE level "
        "WHEN 4 THEN 5 WHEN 3 THEN 4 ELSE level END"
    )
