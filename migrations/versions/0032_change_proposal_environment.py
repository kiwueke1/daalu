"""Record which NV-CM environment a change proposal belongs to.

Device ids are only unique *within* an environment — each one bundles its own
Nautobot — so a proposal that does not name its environment cannot be
re-resolved reliably at execute time. Until now the reconciler and executor
both used the tenant's default source of truth, which meant devices onboarded
into a non-primary environment were never reconciled at all, and an
agent-authored proposal against one would fail ``device_not_found``.

Additive and nullable. NULL keeps the old meaning — "the tenant's default
resolution" — which is correct for every existing row (they can only have been
authored against the default) and stays correct for BYO-Nautobot tenants that
have no environments at all.

Revision ID: 0032_change_proposal_environment
Revises: 0031_config_manager_gateway_url
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_change_proposal_environment"
down_revision = "0031_config_manager_gateway_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "change_proposals",
        sa.Column("environment_slug", sa.String(length=64), nullable=True),
    )
    # The executor looks up open proposals by (tenant, device, environment);
    # the reconciler's "is one already open?" check does the same.
    op.create_index(
        "ix_change_proposals_environment_slug",
        "change_proposals",
        ["environment_slug"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_change_proposals_environment_slug", table_name="change_proposals"
    )
    op.drop_column("change_proposals", "environment_slug")
