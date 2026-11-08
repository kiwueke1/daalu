"""Name the triage timestamp after the TRACE metric it feeds.

``investigations`` gets its first-root-cause timestamp renamed to
``triaged_at`` (time to triage = ``triaged_at - alert.created_at``), and the
per-run timing stored in ``eval_runs.details`` is re-keyed to ``triage_s``,
so the column, the API and the stored eval results all use the TRACE names.

Revision ID: 0035_trace_metric_names
Revises: 0034_aiops_maturity
"""

from __future__ import annotations

from alembic import op

revision = "0035_trace_metric_names"
down_revision = "0034_aiops_maturity"
branch_labels = None
depends_on = None


def _rekey(old: str, new: str) -> None:
    op.execute(
        f"""
        UPDATE eval_runs
           SET details = ((details::jsonb - '{old}')
                          || jsonb_build_object('{new}', details::jsonb -> '{old}'))::json
         WHERE details::jsonb ? '{old}'
        """
    )


def upgrade() -> None:
    op.alter_column("investigations", "diagnosed_at", new_column_name="triaged_at")
    _rekey("diagnosis_s", "triage_s")


def downgrade() -> None:
    _rekey("triage_s", "diagnosis_s")
    op.alter_column("investigations", "triaged_at", new_column_name="diagnosed_at")
