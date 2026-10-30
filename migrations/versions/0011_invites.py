"""Team invites — single-use tokens for inviting users to a tenant.

Revision ID: 0011_invites
Revises: 0010_edge_data_plane
Create Date: 2026-05-25

Implements the Team-tab feature spec'd in book-customer §6. One row
per outstanding or historical invite. The cleartext token is shown
to the inviter exactly once (embedded in the invite URL); only the
sha256 is persisted so a DB leak doesn't yield usable invite
tokens.

A row's lifecycle:

  pending  → accepted_at IS NULL AND revoked_at IS NULL AND now() < expires_at
  expired  → accepted_at IS NULL AND now() >= expires_at
  revoked  → revoked_at IS NOT NULL
  accepted → accepted_at IS NOT NULL  (terminal)

We keep all states (no delete on revoke / accept) so audit can
reconstruct who invited whom.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_invites"
down_revision = "0010_edge_data_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invites",
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
        # The address the invite was sent to. Not unique — the same
        # email may be re-invited if a previous invite was revoked or
        # the previous user offboarded. We enforce "one *pending*
        # invite per (tenant, email)" application-side, not via a DB
        # constraint, so audit history stays intact.
        sa.Column("email", sa.String(320), nullable=False, index=True),
        sa.Column(
            "role",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'user'"),
        ),
        # sha256(cleartext). Lookup at redeem time is by hash; the
        # cleartext is shown to the inviter once (in the invite URL).
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column(
            "invited_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "accepted_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
            "role IN ('admin','user')",
            name="ck_invites_role",
        ),
    )


def downgrade() -> None:
    op.drop_table("invites")
