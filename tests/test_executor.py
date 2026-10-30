"""End-to-end coverage for the executor service (PR 2).

The executor is the single piece of the platform that can push config
to a real device. The tests here pin the two properties that matter:

1. The task only touches rows in status ``approved`` with
   ``executed_at IS NULL`` — pending / failed / rejected / executed /
   stale rows are invisible to it.
2. The whole chain — actor minting → SoT re-render check → adapter
   ``execute()`` — runs against a row and persists a terminal status
   (``executed`` or ``failed`` or ``stale``).

Tests call the async core (``_execute_approved_batch``) directly
rather than going through the ``@celery_app.task`` wrapper, because
the wrapper disposes the global async engine on exit and would tear
down the connection pool the test DB fixture depends on. The Celery
routing surface is verified by a separate assertion on
``celery_app.conf.task_routes``.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import auth_headers  # noqa: F401  — re-export for parity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nautobot_device_payload(
    device_id: str,
    *,
    intent: dict | None = None,
    transport: str = "linux_ssh",
) -> dict:
    config_context: dict[str, dict] = {}
    if intent is not None:
        config_context["daalu_intent"] = intent
    return {
        "id": device_id,
        "name": "web01",
        "primary_ip4": {"address": "10.0.0.5/24"},
        "platform": {"name": "ubuntu"},
        "tags": [],
        "config_context": config_context,
        "custom_field_data": {"daalu_transport": transport},
    }


async def _seed_approved(
    db,
    tenant_id,
    user_id,
    *,
    device_id: str,
    intended_text: str,
    renderer_version: str,
):
    """Convenience: propose+approve in one shot, return the row."""
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.sot.models import Actor
    from daalu_automation.models import ChangeProposalKind

    row = await cps.propose(
        db,
        tenant_id,
        device_id=device_id,
        kind=ChangeProposalKind.manual,
        intended_config=intended_text,
        observed_config="",
        diff="",
        renderer_version=renderer_version,
        evidence={"triggered_by": "user"},
        actor=Actor(kind="user", user_id=user_id),
    )
    await cps.approve(
        db, tenant_id, row.id,
        actor=Actor(kind="user", user_id=user_id),
    )
    return row


# ---------------------------------------------------------------------------
# 1. Happy path — approved row executes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_executor_runs_approved_row_to_executed(
    db, seeded, fake_redis,
    nautobot_integration, ssh_credentials_integration,
    mock_nautobot_transport, mock_asyncssh,
):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.sot.models import LinuxFacts
    from daalu_automation.models import ChangeProposalStatus
    from daalu_automation.workers.executor import _execute_approved_batch

    device_id = str(uuid.uuid4())
    intent_blob = {
        "hostname": "web01",
        "authorized_keys": [],
        "sysctl": [],
        "packages": [],
    }
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {
            "data": {"device": _nautobot_device_payload(device_id, intent=intent_blob)}
        },
    })
    mock_asyncssh({})  # no per-command stdout needed for execute()

    adapter = get_device_adapter("linux_ssh")
    rendered = await adapter.render(LinuxFacts(hostname="web01"))
    intended_text = __import__(
        "daalu_automation.core.change_proposals", fromlist=["serialize_rendered_files"]
    ).serialize_rendered_files(rendered.files)

    row = await _seed_approved(
        db, seeded.tenant_a_id, seeded.user_a_id,
        device_id=device_id,
        intended_text=intended_text,
        renderer_version=rendered.renderer_version,
    )

    stats = await _execute_approved_batch()
    assert stats["executed"] == 1, stats
    assert stats["failed"] == 0
    assert stats["stale"] == 0

    await db.refresh(row)
    assert row.status == ChangeProposalStatus.executed
    assert row.executed_at is not None
    assert (row.executor_result or {}).get("success") is True

    # And the executed event published.
    types = [fields.get("type") for _, _, fields in fake_redis.entries]
    assert "proposal.executed" in types


# ---------------------------------------------------------------------------
# 2. Critical: pending and failed rows are never touched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_executor_leaves_non_approved_rows_alone(
    db, seeded, fake_redis,
    nautobot_integration, ssh_credentials_integration,
    mock_nautobot_transport, mock_asyncssh,
):
    """The SQL filter is the only thing keeping pending rows from being
    auto-approved-and-pushed. Pin it."""
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.sot.models import Actor
    from daalu_automation.models import (
        ChangeProposalKind,
        ChangeProposalStatus,
    )
    from daalu_automation.workers.executor import _execute_approved_batch

    # Pre-seed a pending row.
    pending = await cps.propose(
        db, seeded.tenant_a_id,
        device_id="dev-pending",
        kind=ChangeProposalKind.manual,
        intended_config="### /etc/hostname\nweb-pending\n",
        observed_config="",
        diff="",
        renderer_version="linux_ssh.v1",
        evidence={"triggered_by": "user"},
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )

    # Pre-seed a row already in failed terminal state (e.g., from a
    # previous tick) — must not be retried.
    failed = await cps.propose(
        db, seeded.tenant_a_id,
        device_id="dev-failed",
        kind=ChangeProposalKind.manual,
        intended_config="### /etc/hostname\nweb-failed\n",
        observed_config="",
        diff="",
        renderer_version="linux_ssh.v1",
        evidence={"triggered_by": "user"},
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )
    failed.status = ChangeProposalStatus.failed
    await db.commit()

    # Pre-seed a row already executed.
    already = await cps.propose(
        db, seeded.tenant_a_id,
        device_id="dev-done",
        kind=ChangeProposalKind.manual,
        intended_config="### /etc/hostname\nweb-done\n",
        observed_config="",
        diff="",
        renderer_version="linux_ssh.v1",
        evidence={"triggered_by": "user"},
        actor=Actor(kind="user", user_id=seeded.user_a_id),
    )
    already.status = ChangeProposalStatus.executed
    from datetime import datetime, timezone
    already.executed_at = datetime.now(tz=timezone.utc)
    await db.commit()

    # If Nautobot were ever queried for these device IDs we'd notice
    # — the mock would return an empty device payload and downstream
    # asserts would change. Empty routes default to {"results": [],
    # "data": {}} which would short-circuit cps.execute() with stale.
    mock_nautobot_transport({})
    mock_asyncssh({})

    stats = await _execute_approved_batch()

    # No approved rows → nothing processed.
    assert stats["processed"] == 0, stats
    assert stats["executed"] == 0
    assert stats["failed"] == 0
    assert stats["stale"] == 0

    # Statuses unchanged.
    for row in (pending, failed, already):
        await db.refresh(row)
    assert pending.status == ChangeProposalStatus.pending
    assert pending.executed_at is None
    assert failed.status == ChangeProposalStatus.failed
    assert already.status == ChangeProposalStatus.executed

    # And — critically — no proposal.executed events fired.
    types = [fields.get("type") for _, _, fields in fake_redis.entries]
    assert "proposal.executed" not in types


# ---------------------------------------------------------------------------
# 3. Stale when SoT intent drifted between approve and execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_executor_marks_stale_when_intent_changed(
    db, seeded, fake_redis,
    nautobot_integration, ssh_credentials_integration,
    mock_nautobot_transport, mock_asyncssh,
):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.sot.models import LinuxFacts
    from daalu_automation.models import ChangeProposalStatus
    from daalu_automation.workers.executor import _execute_approved_batch

    device_id = str(uuid.uuid4())
    # Stamp the proposal with rendered intent of hostname=web01, then
    # have the SoT return hostname=web99 — stale-check must fire and
    # the adapter must not be invoked.
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {
            "data": {"device": _nautobot_device_payload(
                device_id, intent={"hostname": "web99"}
            )}
        },
    })

    adapter = get_device_adapter("linux_ssh")

    execute_calls: list[int] = []
    orig_execute = adapter.execute

    async def _spy(*a, **kw):
        execute_calls.append(1)
        return await orig_execute(*a, **kw)

    adapter.execute = _spy  # type: ignore[method-assign]
    try:
        rendered = await adapter.render(LinuxFacts(hostname="web01"))
        intended_text = cps.serialize_rendered_files(rendered.files)

        row = await _seed_approved(
            db, seeded.tenant_a_id, seeded.user_a_id,
            device_id=device_id,
            intended_text=intended_text,
            renderer_version=rendered.renderer_version,
        )

        stats = await _execute_approved_batch()
    finally:
        adapter.execute = orig_execute  # type: ignore[method-assign]

    assert stats["stale"] == 1, stats
    assert stats["executed"] == 0
    assert execute_calls == [], "adapter.execute must not be called when stale"

    await db.refresh(row)
    assert row.status == ChangeProposalStatus.stale


# ---------------------------------------------------------------------------
# 4. Unknown transport — skip without halting the batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_executor_skips_unknown_transport(
    db, seeded, fake_redis,
    nautobot_integration, ssh_credentials_integration,
    mock_nautobot_transport, mock_asyncssh,
):
    """If Nautobot returns a transport string the device-adapter registry
    has never heard of (e.g., a future network-cli transport that
    landed in the SoT before its adapter shipped), the executor must
    log+skip and the row must remain approved for a later tick."""
    from daalu_automation.models import ChangeProposalStatus
    from daalu_automation.workers.executor import _execute_approved_batch

    device_id = str(uuid.uuid4())
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {
            "data": {"device": _nautobot_device_payload(
                device_id,
                intent={"hostname": "web01"},
                transport="cisco_iosxr",  # not registered in v1
            )}
        },
    })
    mock_asyncssh({})

    row = await _seed_approved(
        db, seeded.tenant_a_id, seeded.user_a_id,
        device_id=device_id,
        intended_text="### /etc/hostname\nweb01\n",
        renderer_version="linux_ssh.v1",
    )

    stats = await _execute_approved_batch()
    assert stats["skipped"] >= 1, stats
    assert stats["executed"] == 0

    await db.refresh(row)
    # Row must NOT be marked terminal — operator can re-attempt once
    # the adapter ships.
    assert row.status == ChangeProposalStatus.approved
    assert row.executed_at is None


# ---------------------------------------------------------------------------
# 5. Task routing — confirm the celery surface is wired right
# ---------------------------------------------------------------------------


def test_execute_approved_task_routes_to_executor_queue():
    """The whole identity boundary rests on this routing. Pin it."""
    from daalu_automation.config import get_settings
    from daalu_automation.workers.celery_app import celery_app

    settings = get_settings()
    routes = celery_app.conf.task_routes or {}
    assert "sot.execute_approved" in routes, (
        "sot.execute_approved must be routed to the executor queue so "
        "the main worker pool cannot pick it up"
    )
    assert routes["sot.execute_approved"]["queue"] == settings.executor_queue_name


# ---------------------------------------------------------------------------
# 6. Beat schedule entry — confirm it'll actually fire
# ---------------------------------------------------------------------------


def test_executor_beat_schedule_registered():
    from daalu_automation.workers.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert "sot-execute-approved" in schedule
    assert schedule["sot-execute-approved"]["task"] == "sot.execute_approved"
