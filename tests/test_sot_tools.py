"""Coverage for the propose_change LLM tool.

Drives the handler the way ``kube_tools.execute_tool`` would. The
full alert-chat → Anthropic → tool_use round-trip is exercised by
``test_alert_chat.py``; here we pin the SoT side: that a call lands
exactly one pending ChangeProposal row carrying the model's reasoning
+ evidence, and that error paths return an LLM-readable string rather
than crashing.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select


def _nautobot_device_payload(
    device_id: str,
    *,
    intent: dict | None = None,
    transport: str = "linux_ssh",
    name: str = "web01",
) -> dict:
    cfg = {}
    if intent is not None:
        cfg["daalu_intent"] = intent
    return {
        "id": device_id,
        "name": name,
        "primary_ip4": {"address": "10.0.0.5/24"},
        "platform": {"name": "ubuntu"},
        "tags": [],
        "config_context": cfg,
        "custom_field_data": {"daalu_transport": transport},
    }


# ---------------------------------------------------------------------------
# 1. Happy path — propose_change writes one pending row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_propose_change_writes_pending_row_with_engine_evidence(
    db, seeded, fake_redis,
    nautobot_integration, mock_nautobot_transport,
):
    from daalu_automation.core import kube_tools
    from daalu_automation.models import (
        ChangeProposal,
        ChangeProposalKind,
        ChangeProposalStatus,
    )

    device_id = str(uuid.uuid4())
    current_intent = {
        "hostname": "web01-old",
        "authorized_keys": [],
        "sysctl": [],
        "packages": [],
    }
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {
            "data": {"device": _nautobot_device_payload(
                device_id, intent=current_intent
            )}
        },
    })

    out = await kube_tools.execute_tool(
        "propose_change",
        {
            "device_id": device_id,
            "intended_facts": {
                "hostname": "web01",
                "sysctl": [{"name": "vm.swappiness", "value": "10"}],
            },
            "llm_reasoning": (
                "OOM events correlated with default swappiness=60; "
                "reducing to 10 to bias toward keeping working set "
                "resident."
            ),
            "evidence_event_ids": [str(uuid.uuid4())],
            "evidence_metrics": [
                {"name": "node_memory_SwapFree", "value": "0", "ts": "2026-05-21T00:00:00Z"},
            ],
            "confidence": 0.82,
        },
        tenant_id=seeded.tenant_a_id,
    )

    assert "Created ChangeProposal" in out, out
    assert device_id in out

    rows = (
        await db.execute(
            select(ChangeProposal).where(
                ChangeProposal.tenant_id == seeded.tenant_a_id,
                ChangeProposal.device_id == device_id,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == ChangeProposalStatus.pending
    assert row.kind == ChangeProposalKind.manual
    # Evidence captured for the approver.
    ev = row.evidence
    assert ev["triggered_by"] == "engine"
    assert "swappiness" in ev["llm_reasoning"]
    assert ev["confidence"] == 0.82
    assert len(ev["evidence_events"]) == 1
    assert ev["evidence_metrics"][0]["name"] == "node_memory_SwapFree"
    # Diff non-empty (hostname + sysctl changed vs current intent).
    assert row.diff != ""
    # Renderer version present (executor's stale-check key).
    assert row.renderer_version


# ---------------------------------------------------------------------------
# 2. Missing tenant context — surface a usable error to the LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_propose_change_without_tenant_returns_error(db, fake_redis):
    from daalu_automation.core import kube_tools
    from daalu_automation.models import ChangeProposal

    out = await kube_tools.execute_tool(
        "propose_change",
        {
            "device_id": "dev-x",
            "intended_facts": {"hostname": "web"},
            "llm_reasoning": "ignored",
        },
        tenant_id=None,
    )
    assert out.startswith("error:")
    assert "tenant" in out.lower()

    rows = (await db.execute(select(ChangeProposal))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# 3. Malformed intended_facts — error, no row written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_propose_change_with_bad_facts_returns_error(
    db, seeded, fake_redis, nautobot_integration, mock_nautobot_transport,
):
    from daalu_automation.core import kube_tools
    from daalu_automation.models import ChangeProposal

    mock_nautobot_transport({})  # never reached
    out = await kube_tools.execute_tool(
        "propose_change",
        {
            "device_id": "dev-x",
            # sysctl entries must be {name, value} objects — pass strings
            # so pydantic rejects.
            "intended_facts": {"sysctl": ["vm.swappiness=10"]},
            "llm_reasoning": "this should fail validation",
        },
        tenant_id=seeded.tenant_a_id,
    )
    assert out.startswith("error:")
    assert "LinuxFacts" in out or "sysctl" in out.lower() or "validation" in out.lower()

    rows = (
        await db.execute(
            select(ChangeProposal).where(
                ChangeProposal.tenant_id == seeded.tenant_a_id,
            )
        )
    ).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# 4. Empty reasoning rejected — approvers depend on the explanation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_propose_change_requires_reasoning(
    db, seeded, fake_redis, nautobot_integration, mock_nautobot_transport,
):
    from daalu_automation.core import kube_tools
    from daalu_automation.models import ChangeProposal

    mock_nautobot_transport({})  # never reached
    out = await kube_tools.execute_tool(
        "propose_change",
        {
            "device_id": "dev-x",
            "intended_facts": {"hostname": "web01"},
            "llm_reasoning": "   ",
        },
        tenant_id=seeded.tenant_a_id,
    )
    assert out.startswith("error:")
    assert "reasoning" in out.lower()

    rows = (await db.execute(select(ChangeProposal))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# 5. Unknown device — error, no row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_propose_change_unknown_device_returns_error(
    db, seeded, fake_redis, nautobot_integration, mock_nautobot_transport,
):
    from daalu_automation.core import kube_tools
    from daalu_automation.models import ChangeProposal

    # GraphQL returns null for device → handler should error.
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {"data": {"device": None}},
    })

    out = await kube_tools.execute_tool(
        "propose_change",
        {
            "device_id": "nonexistent",
            "intended_facts": {"hostname": "web01"},
            "llm_reasoning": "should not write a row",
        },
        tenant_id=seeded.tenant_a_id,
    )
    assert out.startswith("error:")
    assert "not found" in out

    rows = (await db.execute(select(ChangeProposal))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# 6. First-time intent (no current Config Context) — diff is the full render
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_propose_change_first_time_intent(
    db, seeded, fake_redis, nautobot_integration, mock_nautobot_transport,
):
    from daalu_automation.core import kube_tools
    from daalu_automation.models import (
        ChangeProposal,
        ChangeProposalStatus,
    )

    device_id = str(uuid.uuid4())
    # No daalu_intent in config_context → first-time intent.
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {
            "data": {"device": _nautobot_device_payload(device_id, intent=None)}
        },
    })

    out = await kube_tools.execute_tool(
        "propose_change",
        {
            "device_id": device_id,
            "intended_facts": {"hostname": "fresh-host"},
            "llm_reasoning": "establishing initial intent for a new device",
        },
        tenant_id=seeded.tenant_a_id,
    )
    assert "Created ChangeProposal" in out

    row = (
        await db.execute(
            select(ChangeProposal).where(
                ChangeProposal.device_id == device_id,
            )
        )
    ).scalar_one()
    assert row.status == ChangeProposalStatus.pending
    assert row.observed_config == ""
    # Diff falls back to the whole intended render — operator sees the full file.
    assert "fresh-host" in row.diff
    assert "/etc/hostname" in row.diff


# ---------------------------------------------------------------------------
# 7. Tool registry — surface is wired right
# ---------------------------------------------------------------------------


def test_propose_change_registered_without_approval_gate():
    from daalu_automation.core import kube_tools

    assert "propose_change" in kube_tools.TOOLS
    spec = kube_tools.TOOLS["propose_change"]
    # The ChangeProposal IS the approval surface — do NOT double-gate.
    assert spec.requires_approval is False
    assert kube_tools.tool_requires_approval("propose_change") is False
    # And it shows up in the Anthropic tool definitions list.
    defs = kube_tools.anthropic_tool_definitions()
    assert any(d["name"] == "propose_change" for d in defs)
    # Required fields are what the LLM actually has to supply.
    pc = next(d for d in defs if d["name"] == "propose_change")
    assert set(pc["input_schema"]["required"]) == {
        "device_id", "intended_facts", "llm_reasoning",
    }
