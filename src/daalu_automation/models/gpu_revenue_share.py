"""Revenue-share ledger — credits the GPU owner for shared-pool usage.

Append-only, written by the inference-gateway alongside each ``usage_events``
row for a daalu-hosted (shared-pool) call. The consumer is billed via
``usage_events`` (unchanged); this ledger records what the *provider* who
owns the serving card earned for it:

    provider_credit_usd = gross_usd * (1 - platform_take_rate)

With a single provider that is also the platform owner the two net out, but
the ledger exists so a second granted provider needs no migration — see
``docs/plans/nvidia-ai-factory/13-gpu-sharing-and-multi-tenant-marketplace.md`` §5.

Never re-priced: like ``usage_events``, the dollar amounts are frozen at
write-time against the rates and take-rate then in force.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import TimestampMixin, UUIDPrimaryKeyMixin


class GpuRevenueShare(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "gpu_revenue_shares"

    # Who consumed the tokens (the billed tenant) and who served them (the
    # credited GPU owner). Both reference tenants; deliberately NOT
    # TenantScopedMixin since the row spans two tenants.
    consumer_tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gpu_pool_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("gpu_pools.id", ondelete="SET NULL"),
        nullable=True,
    )
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Gross = what the consumer was charged for this call (mirrors the
    # usage_events.cost_usd). Credit = the provider's cut after the take.
    gross_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0, nullable=False)
    platform_take_rate: Mapped[float] = mapped_column(
        Numeric(5, 4), default=0, nullable=False
    )
    provider_credit_usd: Mapped[float] = mapped_column(
        Numeric(12, 6), default=0, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
