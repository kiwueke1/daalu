"""Named, multi-instance management environments.

``config_manager_tenants`` was one row per customer, enforced by
``uq_config_manager_tenants_tenant_id``. Environments give a customer several
independent stacks, so that constraint becomes ``(tenant_id, slug)``.

Two things keep this backward compatible:

* **`is_primary`** — the four consumers that resolve "the tenant's config
  manager" (tool proxy, SoT client, NATS bridge, onboarding writer) keep
  working unchanged by resolving the primary. A partial unique index enforces
  at most one primary per customer.
* **Existing rows are left on their original namespace and hostname.** They
  are backfilled with a slug derived from the hostname they already serve, so
  live URLs do not move. Only environments created from here on use the
  ``cm-<tenant>-<env>`` / ``<env>-<tenant>.<domain>`` scheme.

Revision ID: 0030_config_manager_environments
Revises: 0029_workflow_run_alert_steps
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_config_manager_environments"
down_revision = "0029_workflow_run_alert_steps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "config_manager_tenants",
        sa.Column("name", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "config_manager_tenants",
        sa.Column("slug", sa.String(length=24), nullable=False, server_default=""),
    )
    op.add_column(
        "config_manager_tenants",
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )

    # Backfill from the hostname the row already serves: the first DNS label
    # of base_hostname, else the namespace with its `cm-` prefix stripped,
    # else 'default'. This must not change base_hostname or namespace — the
    # existing stack keeps its live URLs.
    op.execute(
        """
        UPDATE config_manager_tenants
           SET slug = COALESCE(
                   NULLIF(split_part(base_hostname, '.', 1), ''),
                   NULLIF(regexp_replace(namespace, '^cm-', ''), ''),
                   'default'
               )
         WHERE slug = ''
        """
    )
    op.execute("UPDATE config_manager_tenants SET name = slug WHERE name = ''")
    # Pre-existing rows are the ones every current consumer already resolves.
    op.execute("UPDATE config_manager_tenants SET is_primary = true")

    op.drop_constraint(
        "uq_config_manager_tenants_tenant_id",
        "config_manager_tenants",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_config_manager_tenants_tenant_slug",
        "config_manager_tenants",
        ["tenant_id", "slug"],
    )
    # At most one primary per customer. Partial index rather than a
    # constraint, so non-primary rows are unconstrained.
    op.create_index(
        "uq_config_manager_tenants_one_primary",
        "config_manager_tenants",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )

    for col in ("name", "slug", "is_primary"):
        op.alter_column("config_manager_tenants", col, server_default=None)


def downgrade() -> None:
    # Only the primary row can survive the old one-per-tenant constraint.
    op.execute("DELETE FROM config_manager_tenants WHERE NOT is_primary")
    op.drop_index(
        "uq_config_manager_tenants_one_primary",
        table_name="config_manager_tenants",
    )
    op.drop_constraint(
        "uq_config_manager_tenants_tenant_slug",
        "config_manager_tenants",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_config_manager_tenants_tenant_id",
        "config_manager_tenants",
        ["tenant_id"],
    )
    op.drop_column("config_manager_tenants", "is_primary")
    op.drop_column("config_manager_tenants", "slug")
    op.drop_column("config_manager_tenants", "name")
