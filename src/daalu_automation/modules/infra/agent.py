"""Infra agent — triages alerts, drafts incident updates, surfaces actions."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from daalu_automation.core.agents import (
    Agent,
    AgentDescriptor,
    emit_alert,
    emit_recommendation,
    register_agent,
)
from daalu_automation.core.alert_fingerprint import compute_fingerprint
from daalu_automation.core.events import EventEnvelope
from daalu_automation.core.llm import LLMUnavailable, complete_json
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import (
    Alert,
    AlertStatus,
    Incident,
    IncidentSeverity,
    IncidentStatus,
)
from daalu_automation.modules.infra.prompts import INCIDENT_TRIAGE_SYSTEM


class InfraAgent(Agent):
    descriptor = AgentDescriptor(
        name="infra-agent",
        module="infra",
        description=(
            "Triages firing alerts, opens incidents, drafts likely root causes "
            "and remediation steps for the on-call SRE."
        ),
        subscribed_event_types=(
            "infra.alert.fired",
            "infra.alert.resolved",
            "infra.incident.opened",
            "infra.deployment.failed",
            "infra.capacity.warning",
        ),
    )

    async def handle(self, event: EventEnvelope) -> None:
        if event.type in ("infra.alert.fired", "infra.deployment.failed"):
            await self._triage_alert(event)
        elif event.type == "infra.alert.resolved":
            await self._auto_resolve_alert(event)
        elif event.type == "infra.capacity.warning":
            await self._recommend_capacity_action(event)

    async def _triage_alert(self, event: EventEnvelope) -> None:
        # Re-fire fast path. A still-firing Alertmanager alert is
        # re-published on every ingest tick. If an open/acknowledged Alert
        # with this fingerprint already exists, just bump its occurrence
        # (emit_alert dedups on the same fingerprint) and skip the
        # expensive quality-tier LLM triage *and* the per-fire incident
        # insert. Only genuinely new alerts get the full treatment — this
        # is what keeps a steady poll affordable and stops infra_incidents
        # growing without bound.
        tenant_uuid = uuid.UUID(event.tenant_id)
        fingerprint = compute_fingerprint(
            module="infra", title=event.summary, metadata=event.payload
        )
        async with AsyncSessionLocal() as db:
            already_open = (
                await db.execute(
                    select(Alert.id)
                    .where(
                        Alert.tenant_id == tenant_uuid,
                        Alert.fingerprint == fingerprint,
                        Alert.status.in_(
                            (AlertStatus.open, AlertStatus.acknowledged)
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        if already_open is not None:
            await emit_alert(
                module="infra",
                severity=getattr(event, "severity", "warning") or "warning",
                title=event.summary,
                body="",
                source_event_id=uuid.UUID(event.event_id),
                metadata=event.payload,
                tenant_id=tenant_uuid,
            )
            return

        try:
            triage = await complete_json(
                system=INCIDENT_TRIAGE_SYSTEM,
                user=self._format_alert(event),
                max_tokens=512,
                tier="quality",
            )
        except LLMUnavailable:
            triage = {
                "likely_root_cause": "AI triage unavailable — configure ANTHROPIC_API_KEY.",
                "remediation": "- Page on-call\n- Check service dashboard\n- Roll back last deploy",
                "blast_radius": "medium",
                "confidence": 0.3,
            }

        severity_map = {
            "low": ("warning", IncidentSeverity.sev3),
            "medium": ("warning", IncidentSeverity.sev2),
            "high": ("critical", IncidentSeverity.sev1),
        }
        alert_severity, incident_severity = severity_map.get(
            triage.get("blast_radius", "medium"), ("warning", IncidentSeverity.sev2)
        )

        # Persist an incident row for the Operations + Alerts pages.
        async with AsyncSessionLocal() as db:
            incident = Incident(
                tenant_id=uuid.UUID(event.tenant_id),
                title=event.summary,
                summary=event.payload.get("description", ""),
                severity=incident_severity,
                status=IncidentStatus.open,
                started_at=datetime.fromisoformat(event.occurred_at),
                ai_root_cause=triage.get("likely_root_cause", ""),
                ai_remediation=triage.get("remediation", ""),
                evidence=[
                    {
                        "kind": "event",
                        "event_id": event.event_id,
                        "summary": event.summary,
                    }
                ],
                external_id=event.payload.get("alert_name"),
            )
            db.add(incident)
            await db.commit()
            await db.refresh(incident)

        await emit_alert(
            module="infra",
            severity=alert_severity,
            title=event.summary,
            body=(
                f"**Likely root cause:** {triage.get('likely_root_cause', '')}\n\n"
                f"**Suggested remediation:**\n{triage.get('remediation', '')}"
            ),
            ai_confidence=float(triage.get("confidence", 0.3)),
            source_event_id=uuid.UUID(event.event_id),
            metadata={"incident_id": str(incident.id), **event.payload},
            tenant_id=uuid.UUID(event.tenant_id),
        )
        if incident_severity == IncidentSeverity.sev1:
            await emit_recommendation(
                module="infra",
                title=f"Escalate Sev-1: {event.summary}",
                rationale=triage.get("likely_root_cause", ""),
                suggested_action="Page on-call manager + open war room",
                confidence=0.9,
                payload={"incident_id": str(incident.id)},
                tenant_id=uuid.UUID(event.tenant_id),
            )

    async def _auto_resolve_alert(self, event: EventEnvelope) -> None:
        """Close any open/acknowledged alert (and its incident) whose firing
        signal has cleared.

        Matches on the fingerprint the resolved event carries — the same key
        ``emit_alert`` dedups fired alerts on — falling back to recomputing it
        from the summary/payload. So a stale alert closes itself the moment the
        monitoring source stops reporting it (e.g. the Prometheus ingest no
        longer sees it firing), instead of lingering open forever.
        """
        tenant_uuid = uuid.UUID(event.tenant_id)
        fingerprint = (event.payload or {}).get("fingerprint")
        if not (isinstance(fingerprint, str) and fingerprint.strip()):
            fingerprint = compute_fingerprint(
                module="infra", title=event.summary, metadata=event.payload
            )
        now = datetime.now(tz=timezone.utc)
        async with AsyncSessionLocal() as db:
            alerts = (
                await db.execute(
                    select(Alert).where(
                        Alert.tenant_id == tenant_uuid,
                        Alert.fingerprint == fingerprint,
                        Alert.status.in_(
                            (AlertStatus.open, AlertStatus.acknowledged)
                        ),
                    )
                )
            ).scalars().all()
            for alert in alerts:
                alert.status = AlertStatus.resolved
                alert.resolved_at = now
                incident_id = (alert.metadata_json or {}).get("incident_id")
                if incident_id:
                    incident = await db.get(Incident, uuid.UUID(incident_id))
                    if incident and incident.status != IncidentStatus.resolved:
                        incident.status = IncidentStatus.resolved
                        incident.resolved_at = now
            if alerts:
                await db.commit()
        self.log.info(
            "infra.alert.auto_resolved",
            fingerprint=fingerprint,
            resolved=len(alerts),
        )

    async def _recommend_capacity_action(self, event: EventEnvelope) -> None:
        await emit_recommendation(
            module="infra",
            title=event.summary,
            rationale=event.payload.get("explanation", "Capacity threshold crossed."),
            suggested_action=event.payload.get(
                "recommendation", "Scale the affected workload by +25% capacity"
            ),
            confidence=0.7,
            payload=event.payload,
            tenant_id=uuid.UUID(event.tenant_id),
        )

    def _format_alert(self, event: EventEnvelope) -> str:
        return json.dumps(
            {
                "type": event.type,
                "summary": event.summary,
                "occurred_at": event.occurred_at,
                "payload": event.payload,
            },
            indent=2,
            default=str,
        )


register_agent(InfraAgent)
