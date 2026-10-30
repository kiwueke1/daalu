"""POST /workflows/run runs as the caller's tenant, whatever the body says.

infra.incident.coordinate reads tenant_id (and incident_id) from its input.
The route used to pass the client's input through untouched, so tenant B
could open an incident in tenant A, or move and re-announce one of A's
incidents on A's Slack/email.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from tests.conftest import auth_headers


@pytest.fixture
def quiet_workflow(monkeypatch):
    """No LLM draft and no Slack/email from the incident workflow."""
    from daalu_automation.modules.infra import workflows as wf

    async def draft(*_a, **_kw):
        return "status update"

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(wf, "_draft_status_update", draft)
    monkeypatch.setattr(wf, "send_slack", noop)
    monkeypatch.setattr(wf, "send_email", noop)


@pytest.mark.asyncio(loop_scope="session")
async def test_new_incident_lands_in_callers_tenant(client, db, seeded, quiet_workflow):
    from daalu_automation.models import Incident

    title = f"cross-tenant probe {uuid.uuid4()}"
    r = await client.post(
        "/api/v1/workflows/run",
        headers=auth_headers(seeded.token_b),
        json={
            "name": "infra.incident.coordinate",
            "input": {"summary": title, "tenant_id": str(seeded.tenant_a_id)},
        },
    )
    assert r.status_code == 202

    rows = (await db.execute(select(Incident).where(Incident.title == title))).scalars().all()
    assert [i.tenant_id for i in rows] == [seeded.tenant_b_id]


@pytest.mark.asyncio(loop_scope="session")
async def test_cannot_update_another_tenants_incident(client, db, seeded, quiet_workflow):
    from daalu_automation.models import Incident, IncidentStatus

    theirs = Incident(
        tenant_id=seeded.tenant_a_id,
        title="tenant A incident",
        summary="",
        started_at=datetime.now(tz=timezone.utc),
    )
    db.add(theirs)
    await db.commit()
    await db.refresh(theirs)
    before = theirs.status

    # The run is recorded as failed and the error propagates ("incident …
    # not found"): tenant A's incident is invisible to tenant B.
    with pytest.raises(ValueError, match="not found"):
        await client.post(
            "/api/v1/workflows/run",
            headers=auth_headers(seeded.token_b),
            json={"name": "infra.incident.coordinate", "input": {"incident_id": str(theirs.id)}},
        )

    await db.refresh(theirs)
    assert theirs.status == before
    assert before != IncidentStatus.investigating
