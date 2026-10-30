"""Quota + rate-limit enforcement for the daalu-hosted tier.

Two separate mechanisms, both run per-request:

* **Monthly quota** — DB-backed, slow path. Reads
  ``daalu_hosted_quotas`` once per request; updates
  ``current_period_used`` *after* the response so a failed call
  doesn't burn budget. Reset to 0 by a Celery beat task at month
  boundary (TODO: ``workers.tasks.reset_monthly_quotas``).

* **Short-period rate limit** — Redis token bucket, fast path. Two
  buckets per tenant: requests-per-minute and tokens-per-minute. The
  RPM bucket is decremented before the call; the TPM bucket is
  decremented by the actual token count after the call returns. A
  tenant exceeding either gets HTTP 429.

The two are complementary: the rate limit prevents one tenant from
starving the pool in the short term; the monthly quota holds the
contract the tenant signed up for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.models.daalu_hosted_quota import DaaluHostedQuota

logger = structlog.get_logger(__name__)


class QuotaError(Exception):
    """Quota/rate-limit refusal.

    ``policy_action`` tells the gateway whether to 429 the client
    (``"deny"``), 429 with a Retry-After hint (``"throttle"``), or
    surrender (``"cloud_overflow"``) so the caller's router can fall
    through to cloud. Always carries the human-readable ``reason``
    for logs.
    """

    def __init__(
        self,
        reason: str,
        policy_action: Literal["deny", "throttle", "cloud_overflow"] = "deny",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.policy_action = policy_action


@dataclass(slots=True)
class QuotaState:
    """Snapshot of the tenant's quota state at request time.

    Carried through the request lifecycle so the post-call metering
    update can write back to the same row without a re-read.
    """

    tenant_id: str
    monthly_token_limit: int
    current_period_used: int
    overage_policy: str
    rate_limit_rpm: int
    rate_limit_tpm: int


async def load_quota(db: AsyncSession, tenant_id: str) -> QuotaState:
    """Load the tenant's quota row. Raises QuotaError if not enabled."""
    stmt = select(DaaluHostedQuota).where(DaaluHostedQuota.tenant_id == tenant_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None or not row.enabled:
        raise QuotaError(
            f"tenant {tenant_id} is not enrolled in the daalu-hosted tier",
            policy_action="deny",
        )
    return QuotaState(
        tenant_id=str(row.tenant_id),
        monthly_token_limit=row.monthly_token_limit,
        current_period_used=row.current_period_used,
        overage_policy=row.overage_policy,
        rate_limit_rpm=row.rate_limit_rpm,
        rate_limit_tpm=row.rate_limit_tpm,
    )


def check_monthly_quota(state: QuotaState) -> None:
    """Refuse if the tenant has burned through its monthly token budget.

    Limit of 0 is interpreted as "unlimited" — this lets us soft-launch
    with no caps before billing reconciles. Set a non-zero limit in the
    row to enforce. The check is approximate (race-free updates happen
    in :func:`record_usage`), but in practice the small slop is fine
    against a monthly budget; nobody complains about being 2k tokens
    over a 50M cap.
    """
    if state.monthly_token_limit == 0:
        return
    if state.current_period_used >= state.monthly_token_limit:
        action: Literal["deny", "cloud_overflow"]
        if state.overage_policy == "cloud_overflow":
            action = "cloud_overflow"
        else:
            action = "deny"
        raise QuotaError(
            f"monthly quota exhausted for tenant {state.tenant_id} "
            f"({state.current_period_used}/{state.monthly_token_limit} tokens)",
            policy_action=action,
        )


async def check_rate_limit(
    redis,  # redis.asyncio.Redis (kept untyped to avoid import cycle in tests)
    state: QuotaState,
    purpose: str,
) -> None:
    """Token-bucket rate limit, decrements the RPM bucket pre-call.

    Bucket key: ``daalu-hosted:rl:rpm:<tenant_id>:<minute_bucket>``.
    Counter increments via INCR + EXPIRE; first hit in a window seeds
    a 65-second TTL so the key naturally retires.

    The TPM bucket is decremented *after* the call by
    :func:`record_usage` because we only know token counts then.
    Pre-call we just check the current value didn't already overflow.
    """
    if state.rate_limit_rpm == 0 and state.rate_limit_tpm == 0:
        return  # disabled
    minute = int(time.time()) // 60
    rpm_key = f"daalu-hosted:rl:rpm:{state.tenant_id}:{minute}"
    tpm_key = f"daalu-hosted:rl:tpm:{state.tenant_id}:{minute}"

    # RPM: atomic incr-and-check.
    if state.rate_limit_rpm > 0:
        count = await redis.incr(rpm_key)
        if count == 1:
            await redis.expire(rpm_key, 65)
        if count > state.rate_limit_rpm:
            raise QuotaError(
                f"rate limit exceeded for tenant {state.tenant_id}: "
                f"{count}/{state.rate_limit_rpm} req/min",
                policy_action="throttle",
            )

    # TPM: just read; the post-call decrement is what enforces.
    if state.rate_limit_tpm > 0:
        used = int((await redis.get(tpm_key)) or 0)
        if used >= state.rate_limit_tpm:
            raise QuotaError(
                f"token-rate limit exceeded for tenant {state.tenant_id}: "
                f"{used}/{state.rate_limit_tpm} tok/min",
                policy_action="throttle",
            )


async def record_usage(
    db: AsyncSession,
    redis,
    state: QuotaState,
    *,
    total_tokens: int,
) -> None:
    """Update post-call counters. Must be called even on partial failures.

    Two writes happen here:

    1. ``daalu_hosted_quotas.current_period_used += total_tokens``
       — the slow, ACID write. UPDATE … SET used = used + :delta so
       concurrent requests serialize cleanly.
    2. Redis TPM bucket INCRBY the same delta — fast write that
       sources the next request's :func:`check_rate_limit` decision.

    If the DB update fails (e.g. connection lost mid-call) we still
    update Redis so the rate limit holds; the missed accounting will
    be picked up by a daily reconciliation task (TODO:
    ``workers.tasks.reconcile_quotas`` reads ``usage_events`` rows
    that didn't make it into ``current_period_used``).
    """
    minute = int(time.time()) // 60
    tpm_key = f"daalu-hosted:rl:tpm:{state.tenant_id}:{minute}"
    try:
        await db.execute(
            DaaluHostedQuota.__table__.update()
            .where(DaaluHostedQuota.tenant_id == state.tenant_id)
            .values(
                current_period_used=DaaluHostedQuota.current_period_used + total_tokens,
                updated_at=datetime.now(tz=timezone.utc),
            )
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — best-effort, see docstring
        logger.exception("quota.persist_failed", tenant_id=state.tenant_id)
        await db.rollback()

    try:
        new = await redis.incrby(tpm_key, total_tokens)
        if new == total_tokens:
            await redis.expire(tpm_key, 65)
    except Exception:  # noqa: BLE001 — Redis transient; next minute's bucket will reset
        logger.exception("quota.redis_failed", tenant_id=state.tenant_id)
