"""Write usage_events rows for daalu-hosted calls.

Same table the rest of the platform writes to. The gateway tags rows
with ``tier='local'`` because the daalu-hosted pool *is* the LOCAL
tier from the router's point of view — see the terminology note at
the top of ``core/llm.py``. ``source`` carries the call's ``purpose``
("chat"/"coding"/…) so the by-source breakdown chart on the billing
page splits along the same lines as the rest of the stack.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import get_settings
from daalu_automation.models.billing import InferenceTier, UsageEvent
from daalu_automation.models.gpu_revenue_share import GpuRevenueShare

logger = structlog.get_logger(__name__)


async def write_usage_event(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    purpose: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
) -> float:
    """Insert one ``usage_events`` row for a successful gateway call.

    Cost is computed at write-time against the daalu-hosted retail
    rates configured on the settings object. Historical rows survive
    future price changes — we do *not* re-price old rows when rates
    move; the bill they generated stands. Returns the computed
    ``cost_usd`` so the caller can use it as the revenue-share gross.
    """
    settings = get_settings()
    cost_usd = (
        prompt_tokens * settings.daalu_hosted_price_in_per_mtok
        + completion_tokens * settings.daalu_hosted_price_out_per_mtok
    ) / 1_000_000.0
    row = UsageEvent(
        tenant_id=tenant_id,
        tier=InferenceTier.LOCAL,
        model=model,
        source=f"daalu-hosted:{purpose}",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
    )
    try:
        db.add(row)
        await db.commit()
    except Exception:  # noqa: BLE001 — best-effort, see app.py comment
        logger.exception(
            "gateway.usage_event_failed",
            tenant_id=tenant_id,
            tokens=prompt_tokens + completion_tokens,
        )
        await db.rollback()
    return cost_usd


def provider_credit(gross_usd: float, take_rate: float) -> float:
    """The GPU owner's cut of a shared-pool call. Pure; clamps take to [0,1]."""
    take = min(max(take_rate, 0.0), 1.0)
    return round(gross_usd * (1.0 - take), 6)


async def record_revenue_share(
    db: AsyncSession,
    *,
    consumer_tenant_id: str,
    provider_tenant_id: str,
    pool_id: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    gross_usd: float,
) -> None:
    """Append one ``gpu_revenue_shares`` row crediting the GPU owner.

    Skipped by the caller when the request was served by the env-var
    fallback (no provider to credit). Best-effort, like the usage event.
    """
    take_rate = float(get_settings().platform_take_rate)
    row = GpuRevenueShare(
        consumer_tenant_id=consumer_tenant_id,
        provider_tenant_id=provider_tenant_id,
        gpu_pool_id=pool_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        gross_usd=round(gross_usd, 6),
        platform_take_rate=take_rate,
        provider_credit_usd=provider_credit(gross_usd, take_rate),
        occurred_at=datetime.now(tz=timezone.utc),
    )
    try:
        db.add(row)
        await db.commit()
    except Exception:  # noqa: BLE001 — best-effort
        logger.exception(
            "gateway.revenue_share_failed",
            provider_tenant_id=provider_tenant_id,
            consumer_tenant_id=consumer_tenant_id,
        )
        await db.rollback()
