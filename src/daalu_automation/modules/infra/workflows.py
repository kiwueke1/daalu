"""Infra coordination workflows."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from daalu_automation.core.events import EventEnvelope, publish
from daalu_automation.core.llm import LLMUnavailable, complete
from daalu_automation.core.notify import send_email, send_slack
from daalu_automation.core.tenant_settings import get_tenant_config
from daalu_automation.core.workflows import register_workflow
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import Incident, IncidentStatus


@register_workflow("infra.incident.coordinate", module="infra")
async def coordinate_incident(payload: dict[str, Any]) -> dict[str, Any]:
    """Open or update an incident, draft a status update, page #incidents.

    Inputs:
      - incident_id (optional)        — existing incident to update
      - summary (required if no id)   — title for a new incident
    """
    incident_id = payload.get("incident_id")
    payload_tenant = payload.get("tenant_id")
    async with AsyncSessionLocal() as db:
        if incident_id:
            incident = await db.get(Incident, uuid.UUID(incident_id))
            # When the caller's tenant is known (always, via the API), an
            # incident from another tenant is treated as not found.
            if incident is None or (
                payload_tenant and str(incident.tenant_id) != str(payload_tenant)
            ):
                raise ValueError(f"incident {incident_id} not found")
        else:
            # New incidents need an explicit tenant — never silently
            # default; that would mis-route a customer's incident to the
            # bootstrap tenant's notification channels.
            if not payload_tenant:
                raise ValueError(
                    "incident.coordinate requires tenant_id when creating "
                    "a new incident"
                )
            incident = Incident(
                tenant_id=uuid.UUID(payload_tenant),
                title=payload["summary"],
                summary=payload.get("description", ""),
                started_at=datetime.now(tz=timezone.utc),
            )
            db.add(incident)
            await db.commit()
            await db.refresh(incident)
        incident.status = IncidentStatus.investigating
        await db.commit()
        title = incident.title
        rc = incident.ai_root_cause or "TBD"
        rem = incident.ai_remediation or "—"
        incident_ref = str(incident.id)
        tenant_id = incident.tenant_id

    status_update = await _draft_status_update(title, rc, rem)

    async with AsyncSessionLocal() as db:
        cfg = await get_tenant_config(db, tenant_id)
        incidents_channel = cfg.slack_incidents_channel or "#incidents"
        await send_slack(
            f":rotating_light: *Incident update — {title}*\n{status_update}",
            tenant_id=tenant_id,
            config=cfg,
            channel=incidents_channel,
        )
        if cfg.incident_email_to:
            await send_email(
                cfg.incident_email_to,
                f"[INCIDENT] {title}",
                f"# Incident update — {title}\n\n{status_update}\n",
                tenant_id=tenant_id,
                config=cfg,
            )
    await publish(
        EventEnvelope(
            type="infra.incident.update",
            module="infra",
            source="workflow",
            severity="warning",
            summary=f"Status update posted for {title}",
            payload={"incident_id": incident_ref, "preview": status_update[:280]},
            # The incident's tenant; the envelope's default is the bootstrap
            # tenant, which filed every tenant's update there.
            tenant_id=str(tenant_id),
        )
    )
    return {"incident_id": incident_ref, "status_update": status_update}


async def _draft_status_update(title: str, root_cause: str, remediation: str) -> str:
    try:
        result = await complete(
            system=(
                "You write internal incident status updates for an SRE team. "
                "3-5 bullets, no marketing language, no apology paragraphs. "
                "Always end with the next status update timestamp."
            ),
            user=(
                f"Incident: {title}\n"
                f"Likely root cause: {root_cause}\n"
                f"Planned remediation: {remediation}\n\n"
                "Draft the status update body."
            ),
            max_tokens=320,
            tier="quality",
        )
        return result.text.strip()
    except LLMUnavailable:
        return (
            f"- *Impact*: {title}\n"
            f"- *Likely cause*: {root_cause}\n"
            f"- *Action*: {remediation}\n"
            f"- *Next update*: in 30 minutes"
        )
