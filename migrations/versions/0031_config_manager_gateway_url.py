"""Publish each environment's in-cluster gateway address on its own row.

The hub tool proxy dials ``gateway_internal_url``, which until now lived only
in the tenant's ``Integration(provider="config_manager")`` config. There is
exactly one such Integration per tenant and it tracks the **primary**
environment, so a second environment had nowhere to publish its gateway and
was unreachable through the proxy.

Additive and nullable: the controller fills it in on the next successful
reconcile (or progress poll), and the Integration copy is left untouched so
the primary path keeps working during the rollout.

Revision ID: 0031_config_manager_gateway_url
Revises: 0030_config_manager_environments
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_config_manager_gateway_url"
down_revision = "0030_config_manager_environments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "config_manager_tenants",
        sa.Column("gateway_internal_url", sa.String(length=512), nullable=True),
    )
    # Seed from the Integration the primary environment already published, so
    # existing stacks stay reachable without waiting for a reconcile tick.
    op.execute(
        """
        UPDATE config_manager_tenants cmt
           SET gateway_internal_url = i.config ->> 'gateway_internal_url'
          FROM integrations i
         WHERE i.tenant_id = cmt.tenant_id
           AND i.provider = 'config_manager'
           AND cmt.is_primary
           AND NULLIF(i.config ->> 'gateway_internal_url', '') IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("config_manager_tenants", "gateway_internal_url")
