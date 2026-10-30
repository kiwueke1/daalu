"""tenants — usable SOVEREIGN inference token + served models.

The pre-existing ``sovereign_inference_token_hash`` is a sha256 (good
for verification, useless for *calling* the endpoint). The UI-driven
GPU onboarding needs the cleartext at call time, so we add a
Fernet-encrypted column the router can decrypt, plus the served model
ids so a tenant's vLLM model is used instead of the operator default.

Revision ID: 0021_tenant_sovereign_inference
Revises: 0020_gpu_tenants
Create Date: 2026-06-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_tenant_sovereign_inference"
down_revision = "0020_gpu_tenants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("sovereign_inference_token_enc", sa.Text, nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("sovereign_model_classifier", sa.String(255), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("sovereign_model_quality", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "sovereign_model_quality")
    op.drop_column("tenants", "sovereign_model_classifier")
    op.drop_column("tenants", "sovereign_inference_token_enc")
