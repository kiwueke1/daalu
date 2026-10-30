"""CLI device authorizations + merge the 0026/0027 heads.

Adds ``cli_device_authorizations`` backing the ``daalu login`` device flow
(see ``models/cli_device.py`` + ``api/routers/cli_auth.py``).

Also a *merge* migration: 0026 (workspace git token) and 0027 (self-signup
email verification) both chain off 0025, leaving two alembic heads. This
revision lists both as down_revisions so ``upgrade head`` resolves to a single
linear head again.

Revision ID: 0028_cli_device_auth
Revises: 0026_workspace_git_token, 0027_self_signup
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# Keep this <= 32 chars — alembic's ``alembic_version.version_num`` is VARCHAR(32).
revision = "0028_cli_device_auth"
down_revision = ("0026_workspace_git_token", "0027_self_signup")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cli_device_authorizations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("device_code_hash", sa.String(length=64), nullable=False),
        sa.Column("user_code", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "pat_id",
            UUID(as_uuid=True),
            sa.ForeignKey("personal_access_tokens.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("client_name", sa.String(length=128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_cli_device_authorizations_device_code_hash",
        "cli_device_authorizations",
        ["device_code_hash"],
        unique=True,
    )
    op.create_index(
        "ix_cli_device_authorizations_user_code",
        "cli_device_authorizations",
        ["user_code"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cli_device_authorizations_user_code",
        table_name="cli_device_authorizations",
    )
    op.drop_index(
        "ix_cli_device_authorizations_device_code_hash",
        table_name="cli_device_authorizations",
    )
    op.drop_table("cli_device_authorizations")
