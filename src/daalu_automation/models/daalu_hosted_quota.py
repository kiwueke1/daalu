"""Per-tenant quota row for the daalu-hosted inference pool.

The daalu-hosted tier is the operator-owned vLLM pool served by the
``inference-gateway`` service. Each tenant that opts in has one row
here; missing row = tier disabled.

Two enforcement layers, separate concerns:

* ``monthly_token_limit`` is the *billing* limit. Long-period,
  consulted on every request, reset by a Celery-beat task at the
  start of each month.
* ``rate_limit_rpm`` / ``rate_limit_tpm`` are the *fairness* limits.
  Short-period, enforced via a Redis token bucket in the gateway.

``overage_policy`` decides what happens when the monthly quota is
exhausted:

* ``hard_stop``     — gateway returns 429. Caller sees the
  ``TierUnavailable`` exception and the router falls through.
* ``throttle``      — gateway slows the response to 1 token/s. Caller
  still gets an answer, just painfully. (Useful for cheap plans.)
* ``cloud_overflow``— gateway returns 429 and the router falls
  through to ``cloud`` without charging the daalu-hosted overage.
  Friendliest default for paid plans where the operator eats the
  small overflow cost.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import TimestampMixin


class DaaluHostedQuota(TimestampMixin, Base):
    __tablename__ = "daalu_hosted_quotas"

    tenant_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    monthly_token_limit: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    current_period_used: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    period_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    overage_policy: Mapped[str] = mapped_column(
        String(32), default="throttle", nullable=False
    )
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    rate_limit_tpm: Mapped[int] = mapped_column(Integer, default=50_000, nullable=False)
