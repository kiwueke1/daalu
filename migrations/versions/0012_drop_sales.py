"""Drop the legacy sales/RevOps tables.

Revision ID: 0012_drop_sales
Revises: 0011_invites
Create Date: 2026-05-25

The platform has been refocused on infrastructure operations. The
``sales_accounts`` / ``sales_leads`` / ``sales_opportunities`` tables
and their two enum types (``leadstatus``, ``opportunitystage``) are
no longer reachable from any code path. This migration drops them.

Tables were never created by an explicit Alembic migration — they
landed on disk via ``Base.metadata.create_all`` in the startup
lifespan hook. This migration uses ``DROP TABLE IF EXISTS`` so the
upgrade is a no-op on databases where the tables were never
materialised.

Downgrade is intentionally empty. There is no recovery path; the
column shapes lived only in the deleted ``models/sales.py``.
"""

from __future__ import annotations

from alembic import op

revision = "0012_drop_sales"
down_revision = "0011_invites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # FKs are CASCADE-from-tenants only; no inbound FKs from kept tables.
    op.execute("DROP TABLE IF EXISTS sales_opportunities CASCADE")
    op.execute("DROP TABLE IF EXISTS sales_leads CASCADE")
    op.execute("DROP TABLE IF EXISTS sales_accounts CASCADE")
    # Enum types from the removed sales.py module. The live DB names
    # them snake-cased (lead_status / opportunity_stage) via the
    # postgresql.ENUM(..., name=...) keyword on the column. Cover
    # both shapes — production uses snake_case; the SQLAlchemy
    # default would have been lower-case-class-name.
    op.execute("DROP TYPE IF EXISTS opportunity_stage")
    op.execute("DROP TYPE IF EXISTS opportunitystage")
    op.execute("DROP TYPE IF EXISTS lead_status")
    op.execute("DROP TYPE IF EXISTS leadstatus")

    # Briefing channel enum loses its ``sales`` value. Delete any
    # briefings written under that channel, then recreate the enum
    # without ``sales``. Postgres has no in-place DROP VALUE so we
    # round-trip through a new type. The live enum is named
    # ``briefing_channel``. Wrap in a DO block guarded by pg_type so
    # the migration is a no-op on fresh installs where the type was
    # never created.
    op.execute("DELETE FROM briefings WHERE channel = 'sales'")
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'briefing_channel') THEN
            ALTER TYPE briefing_channel RENAME TO briefing_channel_old;
            CREATE TYPE briefing_channel AS ENUM (
                'infra','support','finance','operations','hr','executive'
            );
            ALTER TABLE briefings ALTER COLUMN channel TYPE briefing_channel
                USING channel::text::briefing_channel;
            DROP TYPE briefing_channel_old;
        END IF;
    END
    $$;
    """)

    # Users default to briefing_modules = 'infra' going forward. Rewrite
    # rows that still carry the old 'sales,infra' value so the column
    # stays meaningful.
    op.execute(
        "UPDATE users SET briefing_modules = "
        "trim(both ',' from regexp_replace(briefing_modules, "
        "'(^|,)sales(,|$)', '\\1', 'g')) "
        "WHERE briefing_modules ~ '(^|,)sales(,|$)'"
    )
    op.execute(
        "UPDATE users SET briefing_modules = 'infra' WHERE briefing_modules = ''"
    )


def downgrade() -> None:
    """No-op. The schema for sales_* lived in deleted code."""
    pass
