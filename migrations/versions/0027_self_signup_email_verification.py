"""Self-service signup: email verification.

Adds ``users.email_verified_at`` and a single-use
``email_verification_tokens`` table backing the local email+password
signup flow (``core/signup.py`` + ``POST /auth/signup`` /
``POST /auth/verify-email``). A signup creates an inactive user; redeeming
the emailed token sets ``email_verified_at`` and activates the account.

Revision ID: 0027_self_signup
Revises: 0025_workspace_model
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# Keep this <= 32 chars — alembic's ``alembic_version.version_num`` is VARCHAR(32).
revision = "0027_self_signup"
# Chains off the committed migration head (0025). The workspace private-repo
# migration (0026) is separate, still-uncommitted WIP; when it lands it should
# rebase onto 0027 to keep a single linear head.
down_revision = "0025_workspace_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "email_verification_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_email_verification_tokens_user_id",
        "email_verification_tokens",
        ["user_id"],
    )
    op.create_index(
        "ix_email_verification_tokens_token_hash",
        "email_verification_tokens",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_verification_tokens_token_hash",
        table_name="email_verification_tokens",
    )
    op.drop_index(
        "ix_email_verification_tokens_user_id",
        table_name="email_verification_tokens",
    )
    op.drop_table("email_verification_tokens")
    op.drop_column("users", "email_verified_at")
