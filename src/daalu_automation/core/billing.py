"""Billing math and aggregations.

Two halves:

* :func:`compute_cost` — turn a single (tier, tokens, sku) tuple into
  a dollar amount. Called from ``core/llm.py`` after every LLM call.
* :func:`current_period_usage`, :func:`breakdown_by_*` — read-side
  aggregations that the ``/api/v1/billing`` endpoints render.

The shape is deliberately small: there is no separate ledger, no
running balances, no invoice runs. Every dollar amount in the UI is a
sum over ``usage_events`` rows. If we ever need formal invoicing, that
will be a separate table keyed off month-end snapshots — the rows here
are the source of truth.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import DEFAULT_TENANT_ID, get_settings
from daalu_automation.models.billing import (
    InferenceTier,
    RoutingPolicy,
    Sku,
    TenantSku,
    UsageEvent,
)
from daalu_automation.models.gpu_revenue_share import GpuRevenueShare

# ── Cost computation ──────────────────────────────────────────────────────


@dataclass(slots=True)
class _Rates:
    in_per_mtok: float
    out_per_mtok: float


def _rates_for_tier(sku: Sku | None, tier: InferenceTier) -> _Rates:
    """Pull the right (input, output) price pair off the SKU."""
    if sku is None:
        # Fall back to operator-default rates so calls before a tenant has
        # been assigned a SKU still bill against *something* — these get
        # stamped against the default tenant for internal cost tracking.
        s = get_settings()
        if tier is InferenceTier.LOCAL:
            return _Rates(s.llm_local_price_in_per_mtok, s.llm_local_price_out_per_mtok)
        if tier is InferenceTier.EXTERNAL_CLASSIFIER:
            return _Rates(
                s.llm_openai_compat_price_in_per_mtok,
                s.llm_openai_compat_price_out_per_mtok,
            )
        if tier is InferenceTier.EXTERNAL_QUALITY:
            return _Rates(
                s.llm_anthropic_price_in_per_mtok,
                s.llm_anthropic_price_out_per_mtok,
            )
        return _Rates(0.0, 0.0)

    if tier is InferenceTier.LOCAL:
        return _Rates(
            float(sku.price_local_in_per_mtok),
            float(sku.price_local_out_per_mtok),
        )
    if tier is InferenceTier.EXTERNAL_CLASSIFIER:
        return _Rates(
            float(sku.price_external_classifier_in_per_mtok),
            float(sku.price_external_classifier_out_per_mtok),
        )
    if tier is InferenceTier.EXTERNAL_QUALITY:
        return _Rates(
            float(sku.price_external_quality_in_per_mtok),
            float(sku.price_external_quality_out_per_mtok),
        )
    # Sovereign — zero per-call; the customer paid for the hardware.
    return _Rates(0.0, 0.0)


def compute_cost(
    *,
    sku: Sku | None,
    tier: InferenceTier,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Dollar cost of one call against this tier's rates.

    Pure function — does not consult the DB. Caller is expected to have
    the SKU in hand (the routing layer always does, since it had to look
    up the policy to choose a tier).
    """
    rates = _rates_for_tier(sku, tier)
    return round(
        (prompt_tokens / 1_000_000) * rates.in_per_mtok
        + (completion_tokens / 1_000_000) * rates.out_per_mtok,
        6,
    )


# ── Shared-GPU provider earnings ──────────────────────────────────────────


@dataclass(slots=True)
class ProviderEarnings:
    provider_tenant_id: uuid.UUID
    calls: int
    prompt_tokens: int
    completion_tokens: int
    gross_usd: float
    credit_usd: float


async def provider_earnings(
    db: AsyncSession, provider_tenant_id: uuid.UUID
) -> ProviderEarnings:
    """Aggregate the ``gpu_revenue_shares`` ledger for one GPU provider.

    The credit a provider earned for serving other tenants' calls from its
    shared card. Mirrors the read-side billing aggregations — a sum over the
    append-only ledger, no running balance. Powers a provider earnings view.
    """
    row = (
        await db.execute(
            select(
                func.count(GpuRevenueShare.id),
                func.coalesce(func.sum(GpuRevenueShare.prompt_tokens), 0),
                func.coalesce(func.sum(GpuRevenueShare.completion_tokens), 0),
                func.coalesce(func.sum(GpuRevenueShare.gross_usd), 0),
                func.coalesce(func.sum(GpuRevenueShare.provider_credit_usd), 0),
            ).where(GpuRevenueShare.provider_tenant_id == provider_tenant_id)
        )
    ).one()
    return ProviderEarnings(
        provider_tenant_id=provider_tenant_id,
        calls=int(row[0]),
        prompt_tokens=int(row[1]),
        completion_tokens=int(row[2]),
        gross_usd=float(row[3]),
        credit_usd=float(row[4]),
    )


# ── SKU lookup ────────────────────────────────────────────────────────────


async def get_current_sku(
    db: AsyncSession, tenant_id: uuid.UUID | None
) -> Sku | None:
    """The active SKU for this tenant, or None if the tenant has no row.

    Falls back to the operator-default ``Local-First`` SKU if the tenant
    is unset. This keeps background jobs (without a request context)
    billable without forcing every caller to plumb tenant_id through.
    """
    if tenant_id is None:
        tenant_id = DEFAULT_TENANT_ID

    stmt = (
        select(Sku)
        .join(TenantSku, TenantSku.sku_id == Sku.id)
        .where(TenantSku.tenant_id == tenant_id, TenantSku.current.is_(True))
        .limit(1)
    )
    sku = (await db.execute(stmt)).scalar_one_or_none()
    if sku is not None:
        return sku

    # No subscription row yet — fall back to whatever is marked default.
    default_stmt = (
        select(Sku)
        .where(Sku.slug == "local-first", Sku.is_active.is_(True))
        .limit(1)
    )
    return (await db.execute(default_stmt)).scalar_one_or_none()


# ── Aggregations for the UI ───────────────────────────────────────────────


def _period_start(now: datetime | None = None) -> datetime:
    """First moment of the current calendar month, UTC.

    Calendar-month billing is the convention; if we ever offer custom
    cycles (anniversary-month, weekly), this is the seam to replace.
    """
    n = now or datetime.now(tz=timezone.utc)
    return datetime(n.year, n.month, 1, tzinfo=timezone.utc)


@dataclass(slots=True)
class PeriodTotal:
    period_start: datetime
    period_end: datetime
    events: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    base_usd: float
    included_events: int
    included_events_used: int


async def current_period_total(
    db: AsyncSession, tenant_id: uuid.UUID
) -> PeriodTotal:
    """Aggregate the current calendar month for this tenant."""
    start = _period_start()
    now = datetime.now(tz=timezone.utc)

    stmt = (
        select(
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cost_usd), 0),
        )
        .where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.occurred_at >= start,
        )
    )
    events, p_in, p_out, cost = (await db.execute(stmt)).one()

    sku = await get_current_sku(db, tenant_id)
    base = float(sku.monthly_base_usd) if sku else 0.0
    included = int(sku.included_events_per_month) if sku else 0
    used_included = min(events, included)
    return PeriodTotal(
        period_start=start,
        period_end=now,
        events=int(events),
        prompt_tokens=int(p_in),
        completion_tokens=int(p_out),
        cost_usd=float(cost),
        base_usd=base,
        included_events=included,
        included_events_used=used_included,
    )


@dataclass(slots=True)
class BreakdownRow:
    key: str
    events: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


async def breakdown_by_tier(
    db: AsyncSession, tenant_id: uuid.UUID
) -> list[BreakdownRow]:
    """Where did the tokens get manufactured?"""
    start = _period_start()
    stmt = (
        select(
            UsageEvent.tier,
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cost_usd), 0),
        )
        .where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.occurred_at >= start,
        )
        .group_by(UsageEvent.tier)
    )
    rows = (await db.execute(stmt)).all()
    return [
        BreakdownRow(
            key=str(tier.value if hasattr(tier, "value") else tier),
            events=int(events),
            prompt_tokens=int(p_in),
            completion_tokens=int(p_out),
            cost_usd=float(cost),
        )
        for tier, events, p_in, p_out, cost in rows
    ]


async def breakdown_by_source(
    db: AsyncSession, tenant_id: uuid.UUID, limit: int = 10
) -> list[BreakdownRow]:
    """Which agent/briefing/workflow is burning the budget?"""
    start = _period_start()
    stmt = (
        select(
            UsageEvent.source,
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cost_usd), 0),
        )
        .where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.occurred_at >= start,
        )
        .group_by(UsageEvent.source)
        .order_by(func.sum(UsageEvent.cost_usd).desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        BreakdownRow(
            key=source or "unknown",
            events=int(events),
            prompt_tokens=int(p_in),
            completion_tokens=int(p_out),
            cost_usd=float(cost),
        )
        for source, events, p_in, p_out, cost in rows
    ]


@dataclass(slots=True)
class DailyPoint:
    day: str  # YYYY-MM-DD
    events: int
    cost_usd: float


async def daily_series(
    db: AsyncSession, tenant_id: uuid.UUID, days: int = 30
) -> list[DailyPoint]:
    """Per-day rollup for the in-page chart."""
    from datetime import timedelta

    start = datetime.now(tz=timezone.utc) - timedelta(days=days)
    day_col = func.date_trunc("day", UsageEvent.occurred_at).label("day")
    stmt = (
        select(
            day_col,
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.cost_usd), 0),
        )
        .where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.occurred_at >= start,
        )
        .group_by(day_col)
        .order_by(day_col)
    )
    rows = (await db.execute(stmt)).all()
    return [
        DailyPoint(
            day=day.strftime("%Y-%m-%d"),
            events=int(events),
            cost_usd=float(cost),
        )
        for day, events, cost in rows
    ]


# ── Routing policy helpers ────────────────────────────────────────────────


def allows_local(policy: RoutingPolicy) -> bool:
    return policy in (RoutingPolicy.LOCAL_FIRST, RoutingPolicy.HYBRID)


def prefers_local(policy: RoutingPolicy, requested_tier: str) -> bool:
    """Should the router *try local first* for this requested tier?

    ``local_first`` only takes the local path for classifier traffic;
    ``hybrid`` tries local first for both classifier and quality (and
    falls back to external on failure or model-fit).
    """
    if policy is RoutingPolicy.LOCAL_FIRST:
        return requested_tier == "classifier"
    if policy is RoutingPolicy.HYBRID:
        return True
    return False
