"""End-to-end coverage for the SoT / device-management PR-1 surface.

These tests run against a real Postgres (per the repo policy: tests
never mock the DB) but mock the two external transports — Nautobot
HTTP via ``httpx.MockTransport`` and SSH via ``asyncssh.connect``. The
event bus uses an in-memory ``_FakeRedis`` shim (see ``conftest.py``)
so we can verify ``proposal.*`` and ``sot.intent.changed`` events
landed in Postgres without a live broker.

Coverage matrix:

1.  propose() creates a pending row with the right shape
2.  POST /approve flips status + records the approver
3.  POST /reject blocks subsequent execute()
4.  execute() refuses non-executor actors with PermissionError
5.  execute() happy-path: SoT re-render matches, adapter runs, status=executed
6.  execute() flips to stale when re-rendered intent differs
7.  webhook rejects bad HMAC
8.  webhook accepts valid HMAC + publishes sot.intent.changed
9.  reconciler creates a drift proposal when observed != intended
10. reconciler skips devices with an existing open proposal
11. cross-tenant list/get returns 404
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
from sqlalchemy import select

from tests.conftest import auth_headers

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nautobot_device_payload(
    device_id: str,
    *,
    intent: dict | None = None,
    observed: dict | None = None,
) -> dict:
    """Build a Nautobot GraphQL device response with optional contexts."""
    config_context: dict[str, dict] = {}
    if intent is not None:
        config_context["daalu_intent"] = intent
    if observed is not None:
        config_context["daalu_observed"] = observed
    return {
        "id": device_id,
        "name": "web01",
        "primary_ip4": {"address": "10.0.0.5/24"},
        "platform": {"name": "ubuntu"},
        "tags": [],
        "config_context": config_context,
        "custom_field_data": {"daalu_transport": "linux_ssh"},
    }


def _hmac_header(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha512).hexdigest()


# ---------------------------------------------------------------------------
# 1. propose() — service layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_propose_drift_creates_pending_row(db, seeded, fake_redis):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.sot.models import Actor
    from daalu_automation.models import (
        ChangeProposalKind,
        ChangeProposalStatus,
    )

    row = await cps.propose(
        db,
        seeded.tenant_a_id,
        device_id="dev-abc",
        kind=ChangeProposalKind.drift,
        intended_config="### /etc/hostname\nweb01\n",
        observed_config="### /etc/hostname\nold\n",
        diff="-old\n+web01\n",
        renderer_version="linux_ssh.v1",
        evidence={"triggered_by": "reconciler", "confidence": 1.0},
        actor=Actor(kind="system", name="reconciler"),
    )
    assert row.status == ChangeProposalStatus.pending
    assert row.kind == ChangeProposalKind.drift
    assert row.tenant_id == seeded.tenant_a_id
    assert row.device_id == "dev-abc"
    assert row.evidence["triggered_by"] == "reconciler"
    # publish() was invoked → fake redis captured the entry
    assert any("proposal.created" in (fields.get("type") or "") for _, _, fields in fake_redis.entries)
    # And persisted into events
    from daalu_automation.models import Event

    events = (
        await db.execute(
            select(Event).where(
                Event.tenant_id == seeded.tenant_a_id,
                Event.type == "proposal.created",
            )
        )
    ).scalars().all()
    assert len(events) == 1


# ---------------------------------------------------------------------------
# 2. approve — HTTP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_approve_flips_status_and_records_actor(
    db, client, seeded, fake_redis
):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.sot.models import Actor
    from daalu_automation.models import ChangeProposalKind

    row = await cps.propose(
        db,
        seeded.tenant_a_id,
        device_id="dev-xyz",
        kind=ChangeProposalKind.manual,
        intended_config="### /etc/hostname\nweb01\n",
        observed_config="",
        diff="",
        renderer_version="linux_ssh.v1",
        evidence={},
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )
    resp = await client.post(
        f"/api/v1/change-proposals/{row.id}/approve",
        headers=auth_headers(seeded.token_a),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == str(seeded.user_a_id)


# ---------------------------------------------------------------------------
# 3. reject blocks execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_reject_blocks_execute(
    db, seeded, fake_redis
):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot import NautobotSoT
    from daalu_automation.core.sot.models import Actor
    from daalu_automation.models import ChangeProposalKind

    row = await cps.propose(
        db,
        seeded.tenant_a_id,
        device_id="dev-q",
        kind=ChangeProposalKind.manual,
        intended_config="",
        observed_config="",
        diff="",
        renderer_version="linux_ssh.v1",
        evidence={},
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )
    rejected = await cps.reject(
        db,
        seeded.tenant_a_id,
        row.id,
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )
    assert rejected.status.value == "rejected"

    with pytest.raises(cps.ProposalStatusError):
        await cps.execute(
            db,
            seeded.tenant_a_id,
            row.id,
            actor=Actor(kind="executor", scope="executor"),
            sot=NautobotSoT(),
            adapter=get_device_adapter("linux_ssh"),
            creds=Credentials(user="daalu", host="10.0.0.5"),
        )


# ---------------------------------------------------------------------------
# 4. execute refuses non-executor actors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_execute_requires_executor_actor(
    db, seeded, fake_redis
):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot import NautobotSoT
    from daalu_automation.core.sot.models import Actor
    from daalu_automation.models import ChangeProposalKind

    row = await cps.propose(
        db,
        seeded.tenant_a_id,
        device_id="dev-q",
        kind=ChangeProposalKind.manual,
        intended_config="",
        observed_config="",
        diff="",
        renderer_version="linux_ssh.v1",
        evidence={},
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )
    await cps.approve(
        db, seeded.tenant_a_id, row.id,
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )

    # User-kind actor must not be allowed to execute even with the
    # tenant approved.
    with pytest.raises(PermissionError):
        await cps.execute(
            db,
            seeded.tenant_a_id,
            row.id,
            actor=Actor(kind="user", user_id=seeded.user_a_id),
            sot=NautobotSoT(),
            adapter=get_device_adapter("linux_ssh"),
            creds=Credentials(user="daalu", host="10.0.0.5"),
        )

    # Engine actor — also rejected (even though it can propose).
    with pytest.raises(PermissionError):
        await cps.execute(
            db,
            seeded.tenant_a_id,
            row.id,
            actor=Actor(kind="engine", name="infra-agent"),
            sot=NautobotSoT(),
            adapter=get_device_adapter("linux_ssh"),
            creds=Credentials(user="daalu", host="10.0.0.5"),
        )


# ---------------------------------------------------------------------------
# 5. execute happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_execute_happy_path(
    db, seeded, fake_redis, nautobot_integration, mock_nautobot_transport, mock_asyncssh
):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot import NautobotSoT
    from daalu_automation.core.sot.models import Actor
    from daalu_automation.models import ChangeProposalKind

    device_id = str(uuid.uuid4())
    intent_blob = {"hostname": "web01", "authorized_keys": [], "sysctl": [], "packages": []}
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {"data": {"device": _nautobot_device_payload(device_id, intent=intent_blob)}},
    })
    mock_asyncssh({})  # no per-command stdout needed for execute()

    # Snapshot the rendered intent so the row's intended_config matches
    # what NautobotSoT will re-render at execute time.
    adapter = get_device_adapter("linux_ssh")
    from daalu_automation.core.sot.models import LinuxFacts
    rendered = await adapter.render(LinuxFacts(hostname="web01"))
    intended_text = cps.serialize_rendered_files(rendered.files)

    row = await cps.propose(
        db,
        seeded.tenant_a_id,
        device_id=device_id,
        kind=ChangeProposalKind.manual,
        intended_config=intended_text,
        observed_config="",
        diff="",
        renderer_version=rendered.renderer_version,
        evidence={"triggered_by": "user"},
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )
    await cps.approve(
        db, seeded.tenant_a_id, row.id,
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )

    out = await cps.execute(
        db,
        seeded.tenant_a_id,
        row.id,
        actor=Actor(kind="executor", scope="executor"),
        sot=NautobotSoT(),
        adapter=adapter,
        creds=Credentials(
            user="daalu", host="10.0.0.5", private_key_pem="dummy"
        ),
    )
    assert out.status.value == "executed"
    assert out.executor_result.get("success") is True
    # proposal.executed event published
    types = [
        fields.get("type") for _, _, fields in fake_redis.entries
    ]
    assert "proposal.executed" in types


# ---------------------------------------------------------------------------
# 6. execute marks stale when intent drifts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_execute_marks_stale_when_intended_drifted(
    db, seeded, fake_redis, nautobot_integration, mock_nautobot_transport, mock_asyncssh
):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot import NautobotSoT
    from daalu_automation.core.sot.models import Actor, LinuxFacts
    from daalu_automation.models import ChangeProposalKind, ChangeProposalStatus

    device_id = str(uuid.uuid4())
    # SoT will return *new* intent (hostname=web99) — different from
    # what the proposal was snapshotted with (web01) → row must go stale.
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {"data": {"device": _nautobot_device_payload(
            device_id, intent={"hostname": "web99"}
        )}},
    })

    # Track whether the adapter's execute() was called — it must NOT
    # be reached when the proposal goes stale.
    adapter = get_device_adapter("linux_ssh")
    execute_calls: list[int] = []
    orig_execute = adapter.execute

    async def _spy(*a, **kw):
        execute_calls.append(1)
        return await orig_execute(*a, **kw)

    adapter.execute = _spy  # type: ignore[method-assign]

    rendered = await adapter.render(LinuxFacts(hostname="web01"))
    intended_text = cps.serialize_rendered_files(rendered.files)

    row = await cps.propose(
        db,
        seeded.tenant_a_id,
        device_id=device_id,
        kind=ChangeProposalKind.manual,
        intended_config=intended_text,
        observed_config="",
        diff="",
        renderer_version=rendered.renderer_version,
        evidence={"triggered_by": "user"},
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )
    await cps.approve(
        db, seeded.tenant_a_id, row.id,
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )

    with pytest.raises(cps.StaleProposalError):
        await cps.execute(
            db,
            seeded.tenant_a_id,
            row.id,
            actor=Actor(kind="executor", scope="executor"),
            sot=NautobotSoT(),
            adapter=adapter,
            creds=Credentials(
                user="daalu", host="10.0.0.5", private_key_pem="dummy"
            ),
        )
    await db.refresh(row)
    assert row.status == ChangeProposalStatus.stale
    assert execute_calls == []


# ---------------------------------------------------------------------------
# 7. webhook rejects bad HMAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_webhook_rejects_bad_hmac(client, seeded, nautobot_integration, fake_redis):
    resp = await client.post(
        "/api/v1/sot/webhooks/tenant-a",
        content=json.dumps({"event": "updated"}),
        headers={"X-Hook-Signature": "deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 8. webhook accepts valid HMAC + publishes event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_webhook_accepts_valid_hmac_and_publishes_event(
    db, client, seeded, nautobot_integration, fake_redis
):
    from daalu_automation.models import Event

    body_bytes = json.dumps({
        "event": "updated",
        "model": "dcim.device",
        "data": {"id": "abc", "name": "web01"},
    }).encode("utf-8")
    sig = _hmac_header(nautobot_integration.webhook_secret, body_bytes)

    resp = await client.post(
        "/api/v1/sot/webhooks/tenant-a",
        content=body_bytes,
        headers={"X-Hook-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 202, resp.text
    events = (
        await db.execute(
            select(Event).where(
                Event.tenant_id == seeded.tenant_a_id,
                Event.type == "sot.intent.changed",
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].source == "nautobot"


# ---------------------------------------------------------------------------
# 9. reconciler creates a drift proposal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_reconciler_creates_drift_proposal(
    db, seeded, fake_redis,
    nautobot_integration, ssh_credentials_integration,
    mock_nautobot_transport, mock_asyncssh,
):
    from daalu_automation.models import ChangeProposal, ChangeProposalKind
    from daalu_automation.workers.reconciler import _reconcile_all

    device_id = str(uuid.uuid4())
    # Intent says hostname=web01 + sysctl vm.swappiness=10; SSH stub
    # returns hostname=other (drift) and no sysctl value (drift).
    intent_blob = {
        "hostname": "web01",
        "authorized_keys": [],
        "sysctl": [{"name": "vm.swappiness", "value": "10"}],
        "packages": [],
    }
    device_payload = _nautobot_device_payload(device_id, intent=intent_blob)
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {"data": {"devices": [device_payload], "device": device_payload}},
    })
    mock_asyncssh({"hostname": "other-host\n"})

    await _reconcile_all()

    rows = (
        await db.execute(
            select(ChangeProposal).where(
                ChangeProposal.tenant_id == seeded.tenant_a_id,
                ChangeProposal.device_id == device_id,
                ChangeProposal.kind == ChangeProposalKind.drift,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].evidence["triggered_by"] == "reconciler"


# ---------------------------------------------------------------------------
# 10. reconciler skips when a pending proposal already exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_reconciler_skips_when_pending_already_exists(
    db, seeded, fake_redis,
    nautobot_integration, ssh_credentials_integration,
    mock_nautobot_transport, mock_asyncssh,
):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.sot.models import Actor
    from daalu_automation.models import ChangeProposal, ChangeProposalKind
    from daalu_automation.workers.reconciler import _reconcile_all

    device_id = str(uuid.uuid4())
    intent_blob = {
        "hostname": "web01",
        "authorized_keys": [],
        "sysctl": [],
        "packages": [],
    }
    device_payload = _nautobot_device_payload(device_id, intent=intent_blob)
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {"data": {"devices": [device_payload], "device": device_payload}},
    })
    mock_asyncssh({"hostname": "other-host\n"})

    # Pre-seed one pending proposal for this device.
    await cps.propose(
        db,
        seeded.tenant_a_id,
        device_id=device_id,
        kind=ChangeProposalKind.drift,
        intended_config="seed",
        observed_config="",
        diff="",
        renderer_version="linux_ssh.v1",
        evidence={"triggered_by": "reconciler"},
        actor=Actor(kind="system", name="reconciler"),
    )

    await _reconcile_all()

    rows = (
        await db.execute(
            select(ChangeProposal).where(
                ChangeProposal.tenant_id == seeded.tenant_a_id,
                ChangeProposal.device_id == device_id,
            )
        )
    ).scalars().all()
    assert len(rows) == 1  # the pre-seed; no duplicate from reconciler


# ---------------------------------------------------------------------------
# 11. cross-tenant isolation on list / get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_cross_tenant_isolation(
    db, client, seeded, fake_redis
):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.sot.models import Actor
    from daalu_automation.models import ChangeProposalKind

    a_row = await cps.propose(
        db,
        seeded.tenant_a_id,
        device_id="dev-a",
        kind=ChangeProposalKind.manual,
        intended_config="",
        observed_config="",
        diff="",
        renderer_version="linux_ssh.v1",
        evidence={},
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )

    # Tenant B's list must not see A's row.
    resp = await client.get(
        "/api/v1/change-proposals", headers=auth_headers(seeded.token_b)
    )
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == []

    # Tenant B's GET-by-id must 404 (not 403) on A's row.
    resp = await client.get(
        f"/api/v1/change-proposals/{a_row.id}",
        headers=auth_headers(seeded.token_b),
    )
    assert resp.status_code == 404
