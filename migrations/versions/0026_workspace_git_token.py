"""Add ``git_token_encrypted`` to workspaces for private-repo cloning.

A workspace can now be seeded from a *private* GitHub / GitLab /
self-hosted repo. The user supplies a personal-access-token in the
Create-Workspace UI; we store it Fernet-encrypted (``core.crypto``,
keyed off ``secret_key``) so it never sits in plaintext in the DB. The
reconciler decrypts it into a per-workspace K8s Secret that backs the
init-container's git credential helper — see
``workspace_controller/k8s.py``.

Nullable + Text (ciphertext is longer than the raw token). No CHECK
constraint — it's opaque ciphertext, validated only by decrypting.

Revision ID: 0026_workspace_git_token
Revises: 0025_workspace_model
Create Date: 2026-06-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_workspace_git_token"
down_revision = "0025_workspace_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("git_token_encrypted", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "git_token_encrypted")
