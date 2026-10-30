"""Billing tables — SKU catalog, per-tenant subscription, usage events.

Three rows tell the story:

* ``skus`` — the catalog. Owned by the operator, not tenants. Each row
  pins a routing policy ("local_first" vs "external_only" vs …) and the
  pricing the tenant will be charged.
* ``tenant_skus`` — the per-tenant subscription. One row per tenant per
  billing relationship; the ``current`` flag picks the active one.
* ``usage_events`` — append-only row for every LLM call. Tier + model +
  tokens + computed dollars; the routing layer in ``core/llm.py``
  writes one of these per call.

The routing policy on each SKU determines whether ``core/llm.py`` tries
the local NIM first, falls back to external, or refuses local entirely
(the "External-Only" SKU is for customers who don't want their data
crossing the home/operator GPU).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class RoutingPolicy(str, enum.Enum):
    """How a tenant's LLM calls are routed across tiers.

    ``local_first``  — classifier-tier on our NIM, quality-tier on
                       Anthropic. Default. Cheapest unit economics.
    ``hybrid``       — both tiers try local first when a model fits,
                       fall back to external. Best quality+cost mix
                       once a 48GB card joins the pool.
    ``external_only``— never route to a Daalu-operated GPU. For
                       customers with data-residency clauses that
                       forbid traffic through the home/operator host.
    ``sovereign``    — customer's own GPU (federated cluster). Zero
                       per-event cost; flat licence on the SKU.
                       Implementation deferred to PR after federation.
    """

    LOCAL_FIRST = "local_first"
    HYBRID = "hybrid"
    EXTERNAL_ONLY = "external_only"
    SOVEREIGN = "sovereign"


class InferenceTier(str, enum.Enum):
    """Which physical/logical endpoint actually served the call.

    Distinct from the *requested* tier the caller passed (``"quality"`` /
    ``"classifier"``) — that's intent; this is outcome. A caller asks for
    "classifier" and the router may serve it from ``local`` if available
    or ``external_classifier`` if it fell back. Billing is always against
    the *outcome* — you pay for what was actually computed.
    """

    LOCAL = "local"
    EXTERNAL_CLASSIFIER = "external_classifier"
    EXTERNAL_QUALITY = "external_quality"
    SOVEREIGN = "sovereign"


class Sku(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A purchasable plan. Catalog-scoped, not tenant-scoped."""

    __tablename__ = "skus"

    slug: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    tagline: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    routing_policy: Mapped[RoutingPolicy] = mapped_column(
        String(32), nullable=False, default=RoutingPolicy.LOCAL_FIRST
    )

    # Subscription component — flat monthly fee, billed regardless of usage.
    monthly_base_usd: Mapped[float] = mapped_column(
        Numeric(10, 2), default=0, nullable=False
    )
    # Bundle — number of "events" (LLM calls) included in the base.
    # Events beyond this number are billed at the per-event rates below.
    included_events_per_month: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    # Per-million-token rates by tier. Zero = tier not offered on this SKU
    # (the router refuses to use it). Stored as floats for simplicity;
    # accumulated dollar amounts live in Numeric to avoid drift.
    price_local_in_per_mtok: Mapped[float] = mapped_column(
        Numeric(10, 4), default=0, nullable=False
    )
    price_local_out_per_mtok: Mapped[float] = mapped_column(
        Numeric(10, 4), default=0, nullable=False
    )
    price_external_classifier_in_per_mtok: Mapped[float] = mapped_column(
        Numeric(10, 4), default=0, nullable=False
    )
    price_external_classifier_out_per_mtok: Mapped[float] = mapped_column(
        Numeric(10, 4), default=0, nullable=False
    )
    price_external_quality_in_per_mtok: Mapped[float] = mapped_column(
        Numeric(10, 4), default=0, nullable=False
    )
    price_external_quality_out_per_mtok: Mapped[float] = mapped_column(
        Numeric(10, 4), default=0, nullable=False
    )

    # Soft monthly cap. Above this number of dollars, the API starts
    # rejecting non-essential calls. Zero disables the cap.
    monthly_soft_cap_usd: Mapped[float] = mapped_column(
        Numeric(10, 2), default=0, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Display ordering on the pricing page (lower = first).
    display_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)


class TenantSku(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """The plan a tenant is currently on (or has historically been on).

    Multiple rows per tenant are allowed for historical billing — the row
    with ``current=True`` is the active subscription. Switching plans
    flips ``current`` on the old row to false and inserts a new one.
    """

    __tablename__ = "tenant_skus"

    sku_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("skus.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    current: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class UsageEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """One row per LLM call. Drives all billing rollups.

    Written from ``core/llm.py`` *after* the call returns — failed calls
    are not billed. The ``cost_usd`` field is the dollar amount the
    tenant owes for this call, computed at write-time against the
    SKU's per-tier rates so historical rows survive future price
    changes without back-billing.
    """

    __tablename__ = "usage_events"

    tier: Mapped[InferenceTier] = mapped_column(String(32), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    # Caller annotation — e.g. "infra.alert.triage", "infra.brief.daily".
    # Used for the by-source breakdown chart in the UI. Free-form string.
    source: Mapped[str] = mapped_column(String(128), default="", nullable=False, index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0, nullable=False)
    # SKU snapshot — which plan was in force when this call happened. Null
    # only for the pre-billing rows that pre-date this PR.
    sku_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("skus.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # Free-form details — request id, trace id, fallback reason, etc.
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
