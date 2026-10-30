"""Daily IT / Infrastructure / SRE briefing."""

from __future__ import annotations

import json
from typing import Any

from daalu_automation.core.briefings import (
    BriefingContext,
    BriefingGenerator,
    register_briefing,
)
from daalu_automation.models import BriefingChannel
from daalu_automation.modules.infra.prompts import BRIEFING_SYSTEM


class InfraBriefingGenerator(BriefingGenerator):
    channel = BriefingChannel.infra
    module = "infra"

    def system_prompt(self) -> str:
        return BRIEFING_SYSTEM

    def user_prompt(self, ctx: BriefingContext) -> str:
        events_json = [
            {
                "type": e.type,
                "severity": e.severity.value,
                "summary": e.summary,
                "occurred_at": e.occurred_at.isoformat(),
                "payload": e.payload,
            }
            for e in ctx.events
        ]
        return (
            f"Coverage: {ctx.coverage_date.isoformat()} (lookback {ctx.lookback_hours}h)\n"
            f"Event count: {len(ctx.events)}\n\n"
            f"Events JSON:\n{json.dumps(events_json, indent=2, default=str)}\n\n"
            "Produce the JSON briefing now. Do NOT wrap in markdown fences."
        )

    def derive_metrics(self, ctx: BriefingContext, ai_output: dict[str, Any]) -> dict[str, Any]:
        metrics = dict(ai_output.get("metrics", {}))
        metrics.setdefault(
            "incidents_opened",
            sum(1 for e in ctx.events if e.type == "infra.incident.opened"),
        )
        metrics.setdefault(
            "incidents_resolved",
            sum(1 for e in ctx.events if e.type == "infra.incident.resolved"),
        )
        metrics.setdefault(
            "alerts_fired",
            sum(1 for e in ctx.events if e.type == "infra.alert.fired"),
        )
        metrics.setdefault(
            "deployments",
            sum(1 for e in ctx.events if e.type == "infra.deployment.completed"),
        )
        return metrics


register_briefing(InfraBriefingGenerator)
