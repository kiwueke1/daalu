from __future__ import annotations

import enum
from datetime import date

from sqlalchemy import JSON, Date, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import (
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class BriefingChannel(str, enum.Enum):
    infra = "infra"
    # Future modules slot in here without a migration thanks to native enums
    # being re-created during migrations rather than altered.
    support = "support"
    finance = "finance"
    operations = "operations"
    hr = "hr"
    executive = "executive"


class BriefingStatus(str, enum.Enum):
    generating = "generating"
    ready = "ready"
    delivered = "delivered"
    failed = "failed"


class Briefing(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """An AI-generated operational briefing.

    Each briefing is a single document — typically a morning summary —
    rendered for one channel/module. The body is markdown so the UI and
    the Slack/email delivery paths can share content.
    """

    __tablename__ = "briefings"

    channel: Mapped[BriefingChannel] = mapped_column(
        SAEnum(BriefingChannel, name="briefing_channel"),
        nullable=False,
        index=True,
    )
    status: Mapped[BriefingStatus] = mapped_column(
        SAEnum(BriefingStatus, name="briefing_status"),
        default=BriefingStatus.generating,
        nullable=False,
        index=True,
    )
    # Date the briefing covers — yesterday, in tenant local time. Composite
    # natural key together with (tenant_id, channel) prevents duplicate
    # briefings when the scheduler retries.
    coverage_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Structured numbers extracted by the LLM ({"new_leads": 43, ...}) —
    # what the UI renders as metric chips in the hero card.
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # IDs of the source events that fed the briefing — useful for "show me
    # what fed yesterday's report" debugging.
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
