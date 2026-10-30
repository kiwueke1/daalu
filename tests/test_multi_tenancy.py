"""Cross-tenant isolation tests — Slice A of the multi-tenancy rollout.

These exercise the FastAPI app via httpx + ASGITransport so the same
auth middleware that runs in prod runs here. The matrix is:

- detail endpoints (alerts.acknowledge / .resolve, recommendations.approve
  / .dismiss, infra.incident GET) must return 404 — not 403 — when
  called with a JWT for a different tenant, so existence cannot be
  probed by UUID,
- list endpoints (/events, /alerts, /recommendations, /infra/incidents,
  /briefings) must only return rows for the caller's tenant,
- the webhook ingest endpoint (POST /events) must authenticate every
  call against ``tenants.ingest_api_key_hash`` and stamp the resulting
  event with the resolved tenant,
- a JWT for tenant A combined with a forged ``X-Tenant-ID: <B-uuid>``
  header still returns only A's rows (header is ignored),
- ``/api/v1/tenants/*`` requires ``is_superuser``; a tenant-admin gets
  403.

Tests are async and rely on the session-scoped engine + truncate-per-test
session fixtures in conftest.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import auth_headers, ingest_headers

# ---------------------------------------------------------------------------
# Seed helpers — keep each test's body focused on the assertion, not setup
# ---------------------------------------------------------------------------


async def _seed_alert(db, tenant_id, *, title: str):
    from daalu_automation.models import Alert, AlertSeverity, AlertStatus

    alert = Alert(
        tenant_id=tenant_id,
        module="infra",
        severity=AlertSeverity.warning,
        status=AlertStatus.open,
        title=title,
        body=f"body for {title}",
    )
    db.add(alert)
    await db.commit()
    await db.refresh(alert)
    return alert


async def _seed_recommendation(db, tenant_id, *, title: str):
    from daalu_automation.models import Recommendation

    rec = Recommendation(
        tenant_id=tenant_id,
        module="infra",
        title=title,
        rationale="why",
        suggested_action="do thing",
        confidence=0.5,
    )
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return rec


async def _seed_incident(db, tenant_id, *, title: str):
    from daalu_automation.models import Incident, IncidentSeverity, IncidentStatus

    inc = Incident(
        tenant_id=tenant_id,
        title=title,
        summary="",
        severity=IncidentSeverity.sev3,
        status=IncidentStatus.open,
        started_at=datetime.now(tz=timezone.utc),
    )
    db.add(inc)
    await db.commit()
    await db.refresh(inc)
    return inc


async def _seed_event(db, tenant_id, *, summary: str):
    from daalu_automation.models import Event, EventSeverity

    ev = Event(
        tenant_id=tenant_id,
        type="infra.alert.fired",
        module="infra",
        source="test",
        severity=EventSeverity.warning,
        summary=summary,
        occurred_at=datetime.now(tz=timezone.utc),
        payload={},
    )
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    return ev


# ---------------------------------------------------------------------------
# Detail endpoints — cross-tenant access must 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_acknowledge_cross_tenant_404(client, db, seeded):
    """Bob (tenant B) cannot acknowledge Alice's (tenant A) alert.

    404 — not 403 — so the existence of the row isn't even confirmed.
    """
    alice_alert = await _seed_alert(db, seeded.tenant_a_id, title="A's alert")

    r_b = await client.post(
        f"/api/v1/alerts/{alice_alert.id}/acknowledge",
        headers=auth_headers(seeded.token_b),
    )
    assert r_b.status_code == 404, r_b.text

    # Sanity: Alice can ack her own alert.
    r_a = await client.post(
        f"/api/v1/alerts/{alice_alert.id}/acknowledge",
        headers=auth_headers(seeded.token_a),
    )
    assert r_a.status_code == 200, r_a.text


@pytest.mark.asyncio
async def test_alert_resolve_cross_tenant_404(client, db, seeded):
    alice_alert = await _seed_alert(db, seeded.tenant_a_id, title="A's alert")

    r_b = await client.post(
        f"/api/v1/alerts/{alice_alert.id}/resolve",
        headers=auth_headers(seeded.token_b),
    )
    assert r_b.status_code == 404

    r_a = await client.post(
        f"/api/v1/alerts/{alice_alert.id}/resolve",
        headers=auth_headers(seeded.token_a),
    )
    assert r_a.status_code == 200


@pytest.mark.asyncio
async def test_recommendation_approve_cross_tenant_404(client, db, seeded):
    rec_a = await _seed_recommendation(db, seeded.tenant_a_id, title="A's rec")

    r_b = await client.post(
        f"/api/v1/recommendations/{rec_a.id}/approve",
        headers=auth_headers(seeded.token_b),
    )
    assert r_b.status_code == 404


@pytest.mark.asyncio
async def test_recommendation_dismiss_cross_tenant_404(client, db, seeded):
    rec_a = await _seed_recommendation(db, seeded.tenant_a_id, title="A's rec")

    r_b = await client.post(
        f"/api/v1/recommendations/{rec_a.id}/dismiss",
        headers=auth_headers(seeded.token_b),
    )
    assert r_b.status_code == 404


@pytest.mark.asyncio
async def test_incident_get_cross_tenant_404(client, db, seeded):
    inc_a = await _seed_incident(db, seeded.tenant_a_id, title="A's incident")

    r_b = await client.get(
        f"/api/v1/infra/incidents/{inc_a.id}",
        headers=auth_headers(seeded.token_b),
    )
    assert r_b.status_code == 404

    r_a = await client.get(
        f"/api/v1/infra/incidents/{inc_a.id}",
        headers=auth_headers(seeded.token_a),
    )
    assert r_a.status_code == 200
    assert r_a.json()["title"] == "A's incident"


# ---------------------------------------------------------------------------
# List endpoints — only own-tenant rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_list_scoped_to_tenant(client, db, seeded):
    await _seed_event(db, seeded.tenant_a_id, summary="A-event")
    await _seed_event(db, seeded.tenant_b_id, summary="B-event")

    r_a = await client.get("/api/v1/events", headers=auth_headers(seeded.token_a))
    assert r_a.status_code == 200
    summaries_a = [e["summary"] for e in r_a.json()]
    assert "A-event" in summaries_a
    assert "B-event" not in summaries_a

    r_b = await client.get("/api/v1/events", headers=auth_headers(seeded.token_b))
    assert r_b.status_code == 200
    summaries_b = [e["summary"] for e in r_b.json()]
    assert "B-event" in summaries_b
    assert "A-event" not in summaries_b


@pytest.mark.asyncio
async def test_alerts_list_scoped_to_tenant(client, db, seeded):
    await _seed_alert(db, seeded.tenant_a_id, title="A-alert")
    await _seed_alert(db, seeded.tenant_b_id, title="B-alert")

    r_a = await client.get("/api/v1/alerts", headers=auth_headers(seeded.token_a))
    titles_a = [a["title"] for a in r_a.json()]
    assert "A-alert" in titles_a and "B-alert" not in titles_a


@pytest.mark.asyncio
async def test_incidents_list_scoped_to_tenant(client, db, seeded):
    await _seed_incident(db, seeded.tenant_a_id, title="A-incident")
    await _seed_incident(db, seeded.tenant_b_id, title="B-incident")

    r_a = await client.get(
        "/api/v1/infra/incidents", headers=auth_headers(seeded.token_a)
    )
    titles = [i["title"] for i in r_a.json()]
    assert "A-incident" in titles and "B-incident" not in titles


# ---------------------------------------------------------------------------
# Forged X-Tenant-ID is ignored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forged_x_tenant_id_header_is_ignored(client, db, seeded):
    """Phase-1 had ``X-Tenant-ID`` and it was a vector. Confirm it's dead.

    Even if Alice passes a header naming Bob's tenant, the response is
    still scoped to Alice's tenant.
    """
    await _seed_event(db, seeded.tenant_a_id, summary="A-event-forge")
    await _seed_event(db, seeded.tenant_b_id, summary="B-event-forge")

    headers = {
        **auth_headers(seeded.token_a),
        "X-Tenant-ID": str(seeded.tenant_b_id),
    }
    r = await client.get("/api/v1/events", headers=headers)
    assert r.status_code == 200
    summaries = [e["summary"] for e in r.json()]
    assert "A-event-forge" in summaries
    assert "B-event-forge" not in summaries


# ---------------------------------------------------------------------------
# Ingest webhook — per-tenant key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_with_tenant_a_key_stamps_tenant_a(client, db, seeded):
    body = {
        "type": "infra.alert.fired",
        "module": "infra",
        "source": "alertmanager",
        "severity": "critical",
        "summary": "ingest-from-A",
        "payload": {"alert_name": "Test", "service": "x"},
        # Caller tries to spoof another tenant — must be dropped.
        "tenant_id": str(seeded.tenant_b_id),
    }
    r = await client.post(
        "/api/v1/events", json=body, headers=ingest_headers(seeded.ingest_key_a)
    )
    assert r.status_code == 202, r.text

    # Re-read via Alice's list — the event should appear under A.
    r_list_a = await client.get(
        "/api/v1/events", headers=auth_headers(seeded.token_a)
    )
    summaries_a = [e["summary"] for e in r_list_a.json()]
    assert "ingest-from-A" in summaries_a

    # And NOT under B.
    r_list_b = await client.get(
        "/api/v1/events", headers=auth_headers(seeded.token_b)
    )
    summaries_b = [e["summary"] for e in r_list_b.json()]
    assert "ingest-from-A" not in summaries_b


@pytest.mark.asyncio
async def test_ingest_with_missing_key_rejected(client, seeded):
    body = {
        "type": "infra.alert.fired",
        "module": "infra",
        "source": "alertmanager",
        "severity": "warning",
        "summary": "no-key",
        "payload": {},
    }
    r = await client.post("/api/v1/events", json=body)
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_ingest_with_garbage_key_rejected(client, seeded):
    body = {
        "type": "infra.alert.fired",
        "module": "infra",
        "source": "alertmanager",
        "severity": "warning",
        "summary": "bad-key",
        "payload": {},
    }
    r = await client.post(
        "/api/v1/events",
        json=body,
        headers=ingest_headers("not-a-real-key-" + "0" * 60),
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_ingest_with_b_key_stamps_tenant_b(client, db, seeded):
    body = {
        "type": "infra.alert.fired",
        "module": "infra",
        "source": "alertmanager",
        "severity": "warning",
        "summary": "ingest-from-B",
        "payload": {},
    }
    r = await client.post(
        "/api/v1/events", json=body, headers=ingest_headers(seeded.ingest_key_b)
    )
    assert r.status_code == 202

    r_list_a = await client.get(
        "/api/v1/events", headers=auth_headers(seeded.token_a)
    )
    assert "ingest-from-B" not in [e["summary"] for e in r_list_a.json()]

    r_list_b = await client.get(
        "/api/v1/events", headers=auth_headers(seeded.token_b)
    )
    assert "ingest-from-B" in [e["summary"] for e in r_list_b.json()]


# ---------------------------------------------------------------------------
# Tenant CRUD — superuser-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_create_requires_superuser(client, seeded):
    """Alice is a tenant-admin (is_admin=True) but not a superuser.

    She must NOT be able to create tenants — that's a platform-level op.
    """
    r = await client.post(
        "/api/v1/tenants",
        json={"name": "Sneaky", "slug": "sneaky"},
        headers=auth_headers(seeded.token_a),
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_tenant_create_returns_cleartext_ingest_key_once(client, seeded):
    """Cleartext key is part of the create response and never appears again.

    Subsequent GET /tenants/{id} returns ``has_ingest_key: true`` but no
    cleartext — operator must store the value at creation time.
    """
    r = await client.post(
        "/api/v1/tenants",
        json={"name": "Acme", "slug": "acme"},
        headers=auth_headers(seeded.token_super),
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["has_ingest_key"] is True
    cleartext = created["ingest_api_key"]
    assert len(cleartext) == 64  # 32 random bytes → 64 hex chars

    r_get = await client.get(
        f"/api/v1/tenants/{created['id']}",
        headers=auth_headers(seeded.token_super),
    )
    body = r_get.json()
    assert "ingest_api_key" not in body
    assert body["has_ingest_key"] is True

    # The cleartext should work as an ingest key for the new tenant.
    ingest_r = await client.post(
        "/api/v1/events",
        json={
            "type": "infra.alert.fired",
            "module": "infra",
            "source": "alertmanager",
            "severity": "warning",
            "summary": "acme-event",
            "payload": {},
        },
        headers=ingest_headers(cleartext),
    )
    assert ingest_r.status_code == 202


@pytest.mark.asyncio
async def test_tenant_rotate_key_invalidates_old(client, seeded):
    # Original key works.
    body = {
        "type": "infra.alert.fired",
        "module": "infra",
        "source": "alertmanager",
        "severity": "warning",
        "summary": "pre-rotation",
        "payload": {},
    }
    pre = await client.post(
        "/api/v1/events", json=body, headers=ingest_headers(seeded.ingest_key_a)
    )
    assert pre.status_code == 202

    # Rotate.
    rot = await client.post(
        f"/api/v1/tenants/{seeded.tenant_a_id}/rotate-ingest-key",
        headers=auth_headers(seeded.token_super),
    )
    assert rot.status_code == 200, rot.text
    new_key = rot.json()["ingest_api_key"]
    assert new_key != seeded.ingest_key_a

    # Old key 401s.
    body["summary"] = "post-rotation-old-key"
    old = await client.post(
        "/api/v1/events", json=body, headers=ingest_headers(seeded.ingest_key_a)
    )
    assert old.status_code == 401

    # New key works.
    body["summary"] = "post-rotation-new-key"
    new = await client.post(
        "/api/v1/events", json=body, headers=ingest_headers(new_key)
    )
    assert new.status_code == 202


@pytest.mark.asyncio
async def test_tenant_delete_locks_out_ingest(client, seeded):
    """Soft-delete clears the ingest key hash — shims fail closed."""
    r = await client.delete(
        f"/api/v1/tenants/{seeded.tenant_a_id}",
        headers=auth_headers(seeded.token_super),
    )
    assert r.status_code == 204

    body = {
        "type": "infra.alert.fired",
        "module": "infra",
        "source": "alertmanager",
        "severity": "warning",
        "summary": "post-delete",
        "payload": {},
    }
    locked = await client.post(
        "/api/v1/events", json=body, headers=ingest_headers(seeded.ingest_key_a)
    )
    assert locked.status_code == 401


@pytest.mark.asyncio
async def test_tenant_list_excludes_soft_deleted(client, seeded):
    # Delete A, list as superuser, expect only B + (the superuser's own tenant
    # was A, but we just deleted it). Actually the superuser is in tenant A —
    # delete leaves the user row intact since is_deleted only flags the
    # tenant. So listing should return only tenant B + whatever else exists.
    r = await client.delete(
        f"/api/v1/tenants/{seeded.tenant_a_id}",
        headers=auth_headers(seeded.token_super),
    )
    assert r.status_code == 204

    listed = await client.get(
        "/api/v1/tenants",
        headers=auth_headers(seeded.token_super),
    )
    assert listed.status_code == 200
    slugs = [t["slug"] for t in listed.json()]
    assert "tenant-a" not in slugs
    assert "tenant-b" in slugs


@pytest.mark.asyncio
async def test_unknown_tenant_404(client, seeded):
    r = await client.get(
        f"/api/v1/tenants/{uuid.uuid4()}",
        headers=auth_headers(seeded.token_super),
    )
    assert r.status_code == 404
