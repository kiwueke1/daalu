"""Pluggable briefing generators.

A briefing is an AI-rendered operational document. Modules register one
or more ``BriefingGenerator`` subclasses; the scheduler walks the
registry every morning and produces one ``Briefing`` row per
(tenant, channel).

The base class handles persistence + delivery (Slack/email) — concrete
subclasses only need to assemble the LLM prompt from yesterday's events.
"""

from __future__ import annotations

import abc
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import DEFAULT_TENANT_ID, get_settings
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import Briefing, BriefingChannel, BriefingStatus, Event

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class BriefingContext:
    tenant_id: uuid.UUID
    channel: BriefingChannel
    coverage_date: date
    lookback_hours: int
    events: list[Event]


class BriefingGenerator(abc.ABC):
    channel: BriefingChannel
    module: str
    title_template: str = "{channel_label} briefing — {date}"

    # ── Subclasses implement these two ────────────────────────────────────
    @abc.abstractmethod
    def system_prompt(self) -> str: ...

    @abc.abstractmethod
    def user_prompt(self, ctx: BriefingContext) -> str: ...

    # Override to filter the events that get fed into the LLM. Default is
    # "every event for the module in the lookback window".
    def relevant_event_filter(self):
        return Event.module == self.module

    # Override to extract structured metric chips the UI renders.
    def derive_metrics(self, ctx: BriefingContext, ai_output: dict[str, Any]) -> dict[str, Any]:
        return ai_output.get("metrics", {})

    # ── Pipeline ──────────────────────────────────────────────────────────
    async def generate(self, *, tenant_id: uuid.UUID | None = None) -> Briefing:
        settings = get_settings()
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        coverage_date = (datetime.now(tz=timezone.utc) - timedelta(days=1)).date()
        async with AsyncSessionLocal() as db:
            briefing = await self._begin(db, tenant_id, coverage_date)
            try:
                ctx = await self._load_context(
                    db, tenant_id, coverage_date, settings.briefing_lookback_hours
                )
                ai_output = await self._call_llm(ctx)
                briefing.title = self.title_template.format(
                    channel_label=self.channel.value.title(), date=coverage_date.isoformat()
                )
                briefing.summary = ai_output.get("summary", "")[:1024]
                briefing.body_markdown = ai_output.get("body", "")
                briefing.metrics = self.derive_metrics(ctx, ai_output)
                briefing.source_event_ids = [str(e.id) for e in ctx.events]
                briefing.status = BriefingStatus.ready
                await db.commit()
                await db.refresh(briefing)
                return briefing
            except Exception as e:  # noqa: BLE001
                briefing.status = BriefingStatus.failed
                briefing.error_message = str(e)
                await db.commit()
                raise

    async def _begin(
        self, db: AsyncSession, tenant_id: uuid.UUID, coverage_date: date
    ) -> Briefing:
        # Idempotency — replace today's briefing if the scheduler retries.
        existing = (
            await db.execute(
                select(Briefing).where(
                    and_(
                        Briefing.tenant_id == tenant_id,
                        Briefing.channel == self.channel,
                        Briefing.coverage_date == coverage_date,
                    )
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.status = BriefingStatus.generating
            existing.error_message = None
            await db.commit()
            return existing
        briefing = Briefing(
            tenant_id=tenant_id,
            channel=self.channel,
            coverage_date=coverage_date,
            title=self.title_template.format(
                channel_label=self.channel.value.title(),
                date=coverage_date.isoformat(),
            ),
        )
        db.add(briefing)
        await db.commit()
        await db.refresh(briefing)
        return briefing

    async def _load_context(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        coverage_date: date,
        lookback_hours: int,
    ) -> BriefingContext:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)
        events = (
            (
                await db.execute(
                    select(Event)
                    .where(
                        and_(
                            Event.tenant_id == tenant_id,
                            Event.occurred_at >= cutoff,
                            self.relevant_event_filter(),
                        )
                    )
                    .order_by(Event.occurred_at.desc())
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
        return BriefingContext(
            tenant_id=tenant_id,
            channel=self.channel,
            coverage_date=coverage_date,
            lookback_hours=lookback_hours,
            events=list(events),
        )

    async def _call_llm(self, ctx: BriefingContext) -> dict[str, Any]:
        # Falls back to a deterministic "no LLM configured" body so the
        # platform stays demo-able without an Anthropic key.
        from daalu_automation.core.llm import LLMUnavailable, complete_json

        try:
            return await complete_json(
                system=self.system_prompt(),
                user=self.user_prompt(ctx),
                max_tokens=2048,
            )
        except LLMUnavailable:
            return self._offline_fallback(ctx)

    def _offline_fallback(self, ctx: BriefingContext) -> dict[str, Any]:
        bullets = "\n".join(
            f"- {e.occurred_at:%H:%M}  [{e.severity.value}] {e.summary}" for e in ctx.events[:20]
        )
        return {
            "summary": f"{len(ctx.events)} {self.module} events in the last "
            f"{ctx.lookback_hours}h. Configure ANTHROPIC_API_KEY for AI summarisation.",
            "body": f"## Recent {self.module} activity\n\n{bullets or '_No events._'}",
            "metrics": {"events": len(ctx.events)},
        }


# ── Registry ─────────────────────────────────────────────────────────────
_GENERATORS: dict[BriefingChannel, Callable[[], BriefingGenerator]] = {}


def register_briefing(factory: Callable[[], BriefingGenerator]) -> Callable[[], BriefingGenerator]:
    instance = factory()
    _GENERATORS[instance.channel] = factory
    logger.info("briefing.registered", channel=instance.channel.value)
    return factory


def list_briefings() -> list[BriefingChannel]:
    return list(_GENERATORS.keys())


def get_briefing_generator(channel: BriefingChannel) -> BriefingGenerator:
    return _GENERATORS[channel]()
