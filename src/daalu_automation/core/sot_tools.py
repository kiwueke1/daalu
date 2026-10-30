"""LLM-callable tools that interact with the Source of Truth.

Two families:

* **Read** — :func:`_list_devices`, :func:`_get_device`,
  :func:`_get_fabric_topology`. These are what let the copilot answer
  "what does my infrastructure look like?" against the *real* inventory
  instead of guessing. They are the tools ``propose_change``'s own
  schema has always told the model to look devices up with; until they
  existed that instruction pointed at nothing.
* **Write** — :func:`_propose_change`. The engine emits a
``propose_change`` tool call during an alert investigation; the
handler renders the desired Linux facts via the device adapter,
captures the LLM's reasoning + evidence on the proposal row, and
writes a ``pending`` ``ChangeProposal``.

``requires_approval`` is **False** — not because the change is safe,
but because the ChangeProposal *is* the approval surface. The operator
approves the proposal in the dedicated Proposals UI, not in the
alert-chat action card.  Double-gating would make the chat card claim
authority it doesn't have (its approve button would not execute the
push — only flipping the proposal to ``approved`` does that, and the
executor is the only thing that can push).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from daalu_automation.config import get_settings
from daalu_automation.core.sot.models import LinuxFacts

logger = logging.getLogger(__name__)


PROPOSE_CHANGE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "device_id": {
            "type": "string",
            "description": (
                "Nautobot device UUID. Look it up via list_devices / "
                "the SoT before calling this tool — do not invent IDs."
            ),
        },
        "intended_facts": {
            "type": "object",
            "description": (
                "The desired LinuxFacts to converge the device toward. "
                "Schema matches the LinuxFacts model: hostname (str), "
                "authorized_keys (list of {user, key}), sysctl (list of "
                "{name, value}), packages (list of {name, state}), "
                "cloud_init ({content}). Omit any fact you don't intend "
                "to manage — only the keys you set will be diffed."
            ),
            "properties": {
                "hostname": {"type": "string"},
                "authorized_keys": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "user": {"type": "string"},
                            "key": {"type": "string"},
                        },
                        "required": ["user", "key"],
                    },
                },
                "sysctl": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["name", "value"],
                    },
                },
                "packages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "state": {
                                "type": "string",
                                "enum": ["present", "absent"],
                            },
                        },
                        "required": ["name"],
                    },
                },
                "cloud_init": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                },
            },
        },
        "llm_reasoning": {
            "type": "string",
            "description": (
                "1–3 sentence explanation of why this change is being "
                "proposed. Surfaced verbatim to the operator on the "
                "approval card — this is the single most important "
                "argument for the approver."
            ),
        },
        "environment": {
            "type": "string",
            "description": (
                "NV-CM management environment slug the device belongs to. "
                "Each environment has its own device inventory, so pass the "
                "slug you saw the device under. Omit it only when the "
                "tenant has a single (primary) environment."
            ),
        },
        "evidence_event_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "UUIDs of Event rows that motivated the change. The UI "
                "renders these as clickable links back into the events "
                "stream."
            ),
        },
        "evidence_alert_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "UUIDs of Alert rows related to this proposal.",
        },
        "evidence_metrics": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "Metric samples or queries that motivated the change. "
                "Free-form dicts — typical shape: "
                "{name, value, ts, source}."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": (
                "Self-reported confidence in this proposal (0–1). Helps "
                "the operator triage a long approval queue."
            ),
        },
    },
    "required": ["device_id", "intended_facts", "llm_reasoning"],
}


async def _propose_change(
    *,
    device_id: str,
    intended_facts: dict[str, Any],
    llm_reasoning: str,
    evidence_event_ids: list[str] | None = None,
    evidence_alert_ids: list[str] | None = None,
    evidence_metrics: list[dict[str, Any]] | None = None,
    confidence: float | None = None,
    environment: str | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Author a pending :class:`ChangeProposal` for operator approval.

    Looks the device up on the SoT, renders both the current intent
    (for the observed-config snapshot baseline) and the proposed intent
    (for the intended-config snapshot the executor will compare against
    at execute time), computes a unified diff, and writes the row with
    ``kind="manual"`` and ``Actor(kind="engine", name="infra-agent")``.

    The full ``llm_reasoning`` + evidence go onto ``ChangeProposal.evidence``
    so the approval UI can render the model's argument directly. Returns
    a short confirmation containing the new proposal ID; the operator
    approves via the Proposals page, not via the alert chat.
    """
    # Imports inside the function body — the alert chat module imports
    # this file at startup, before the DB engine / Anthropic client are
    # configured. Lazy-import keeps test-time fixtures (which patch
    # env before importing daalu_automation modules) authoritative.
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.sot import NautobotSoT, NautobotUnavailable
    from daalu_automation.core.sot.models import Actor
    from daalu_automation.database import AsyncSessionLocal
    from daalu_automation.models import ChangeProposalKind

    if _tenant_id is None:
        return "error: tenant context missing for propose_change"

    try:
        facts = LinuxFacts(**intended_facts)
    except Exception as e:  # noqa: BLE001 — pydantic raises ValidationError; surface to LLM
        return f"error: intended_facts did not match LinuxFacts schema — {e}"

    if not (llm_reasoning or "").strip():
        return "error: llm_reasoning is required (the approver reads it)"

    # Resolve the device in its own environment's Nautobot — the id is only
    # unique within one — and stamp the slug on the proposal so the executor
    # re-resolves it in the same place at apply time.
    sot = NautobotSoT(env=environment)
    async with AsyncSessionLocal() as db:
        try:
            device = await sot.get_device(db, _tenant_id, device_id)
        except NautobotUnavailable as e:
            return f"error: nautobot not reachable — {e}"
        if device is None:
            return f"error: device {device_id!r} not found in this tenant's SoT"

        try:
            adapter = get_device_adapter(device.transport)
        except KeyError:
            return (
                f"error: no adapter registered for device transport "
                f"{device.transport!r} — supported transports are linux_ssh"
            )

        rendered_intended = await adapter.render(facts)
        intended_text = cps.serialize_rendered_files(rendered_intended.files)

        # Diff baseline: what the SoT *currently* intends. This is what
        # the operator sees as "observed_config" on the proposal — not a
        # live device read, but the canonical prior state the model
        # wants to change. The drift reconciler is the path that
        # compares to actual device state.
        try:
            current_intent = await sot.get_intended_config(db, _tenant_id, device_id)
        except NautobotUnavailable:
            current_intent = None

        if current_intent is not None:
            rendered_current = await adapter.render(current_intent.facts)
            observed_text = cps.serialize_rendered_files(rendered_current.files)
            cdiff = await adapter.diff(current_intent.facts, facts)
            diff_text = cdiff.unified_diff if cdiff.has_changes else ""
        else:
            # First-time intent for this device — there's nothing to
            # diff against. Show the whole rendered config as the diff
            # so the operator sees what will be written.
            observed_text = ""
            diff_text = intended_text

        settings = get_settings()
        evidence_payload: dict[str, Any] = {
            "triggered_by": "engine",
            "llm_reasoning": llm_reasoning,
            "llm_model": settings.anthropic_model or None,
            "evidence_events": [str(e) for e in (evidence_event_ids or [])],
            "evidence_alerts": [str(a) for a in (evidence_alert_ids or [])],
            # evidence_metrics stays free-form (list[dict]) per the
            # sot-pr2-decisions memo — typing it here adds schema work
            # without changing what reviewers actually see.
            "evidence_metrics": list(evidence_metrics or []),
        }
        if confidence is not None:
            evidence_payload["confidence"] = float(confidence)
        evidence_payload["proposed_at"] = datetime.now(tz=timezone.utc).isoformat()

        row = await cps.propose(
            db,
            _tenant_id,
            device_id=device_id,
            environment_slug=environment,
            kind=ChangeProposalKind.manual,
            intended_config=intended_text,
            observed_config=observed_text,
            diff=diff_text,
            renderer_version=rendered_intended.renderer_version,
            evidence=evidence_payload,
            actor=Actor(kind="engine", name="infra-agent"),
        )

    return (
        f"Created ChangeProposal {row.id} for device {device.name} "
        f"({device_id}). Status: pending. The operator must approve "
        f"this proposal via the Proposals UI before the executor will "
        f"push to the device."
    )


# ---------------------------------------------------------------------------
# Read tools
#
# Every handler returns a plain string: ``execute_tool`` hands it straight
# back to the model as the tool result, so the format is a prompt, not an
# API response. Compact tables beat JSON here — they cost fewer tokens and
# models read them more reliably.
#
# All three resolve the environment the same way ``propose_change`` does:
# ``NautobotSoT(env=...)``. A device id is only unique within one
# environment's Nautobot, so ``environment`` has to travel with every call.
# ---------------------------------------------------------------------------

# Hard cap on rows returned to the model. A fabric far larger than the
# lab's twelve devices would otherwise blow the context window on a single
# list_devices call; the model is told when it has been truncated so it can
# narrow with a filter instead of silently reasoning over a partial list.
_MAX_ROWS = 200


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


async def _resolve_device(sot: Any, db: Any, tenant_id: uuid.UUID, ref: str) -> Any:
    """Find a device by UUID *or* by name.

    Operators — and therefore the model relaying them — say "leaf1", not
    a UUID. Nautobot's get-by-id is the only direct lookup, so a
    non-UUID reference falls back to a name match over the device list.
    Case-insensitive because the spreadsheet and the CLI disagree often
    enough to matter.
    """
    if _looks_like_uuid(ref):
        return await sot.get_device(db, tenant_id, ref)
    wanted = (ref or "").strip().lower()
    for dev in await sot.list_devices(db, tenant_id):
        if dev.name.lower() == wanted:
            return dev
    return None


async def _list_devices(
    *,
    platform: str | None = None,
    role: str | None = None,
    name_contains: str | None = None,
    environment: str | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """List the devices in the tenant's source of truth."""
    from daalu_automation.core.sot import NautobotSoT, NautobotUnavailable
    from daalu_automation.database import AsyncSessionLocal

    if _tenant_id is None:
        return "error: tenant context missing for list_devices"

    sot = NautobotSoT(env=environment)
    async with AsyncSessionLocal() as db:
        try:
            devices = await sot.list_devices(db, _tenant_id, platform=platform)
        except NautobotUnavailable as e:
            return (
                f"error: source of truth not reachable — {e}. The environment may "
                "not be provisioned yet, or its Nautobot is still starting."
            )

    # Role lives in tags for the SoT-native model; match leniently so the
    # model can ask for "spine" without knowing how it is stored.
    if role:
        want = role.strip().lower()
        devices = [
            d
            for d in devices
            if want in [t.lower() for t in d.tags]
            or want == str(d.extra.get("role", "")).lower()
        ]
    if name_contains:
        needle = name_contains.strip().lower()
        devices = [d for d in devices if needle in d.name.lower()]

    if not devices:
        return (
            "No devices matched. If the environment was just provisioned its "
            "source of truth may still be empty — devices appear after a bulk "
            "import."
        )

    truncated = len(devices) > _MAX_ROWS
    rows = devices[:_MAX_ROWS]
    lines = [f"{len(devices)} device(s) in the source of truth:", ""]
    lines.append("name | primary_ip | platform | transport | role/tags | id")
    for d in rows:
        role_s = str(d.extra.get("role", "")) or ",".join(d.tags) or "-"
        lines.append(
            f"{d.name} | {d.primary_ip or '-'} | {d.platform} | "
            f"{d.transport} | {role_s} | {d.id}"
        )
    if truncated:
        lines.append(
            f"\n(showing first {_MAX_ROWS} of {len(devices)} — narrow with "
            "role, platform or name_contains)"
        )
    return "\n".join(lines)


async def _get_device(
    *,
    device: str,
    environment: str | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """Full detail for one device, by name or UUID."""
    from daalu_automation.core.sot import NautobotSoT, NautobotUnavailable
    from daalu_automation.database import AsyncSessionLocal

    if _tenant_id is None:
        return "error: tenant context missing for get_device"

    sot = NautobotSoT(env=environment)
    async with AsyncSessionLocal() as db:
        try:
            dev = await _resolve_device(sot, db, _tenant_id, device)
        except NautobotUnavailable as e:
            return f"error: source of truth not reachable — {e}"

    if dev is None:
        return (
            f"error: no device named or identified by {device!r} in this "
            "environment's source of truth. Call list_devices to see what exists."
        )

    lines = [
        f"name        : {dev.name}",
        f"id          : {dev.id}",
        f"primary_ip  : {dev.primary_ip or '-'}",
        f"platform    : {dev.platform}",
        f"transport   : {dev.transport}",
        f"tags        : {', '.join(dev.tags) or '-'}",
    ]
    for k, v in (dev.extra or {}).items():
        lines.append(f"{k:12}: {v}")
    return "\n".join(lines)


async def _get_fabric_topology(
    *,
    device: str | None = None,
    include_mgmt: bool = False,
    environment: str | None = None,
    _tenant_id: uuid.UUID | None = None,
) -> str:
    """The cabling graph: which device ports connect to which.

    Built from the live LLDP sweep, not from the inventory upload — so
    this answers "what is actually plugged in", which is a different and
    stronger claim than "what did the spreadsheet say".
    """
    from daalu_automation.core.topology.service import build_topology
    from daalu_automation.database import AsyncSessionLocal

    if _tenant_id is None:
        return "error: tenant context missing for get_fabric_topology"

    async with AsyncSessionLocal() as db:
        try:
            graph = await build_topology(db, _tenant_id, environment)
        except Exception as e:  # noqa: BLE001 — surface any backend fault to the model
            return f"error: could not build topology — {type(e).__name__}: {e}"

    # TopologyLink carries device *ids* in source/target; the model needs
    # names. Without this map every row renders as "?" and the answer is
    # useless even though the graph is correct.
    names = {n.id: (n.name or n.id) for n in (graph.nodes or [])}
    links = list(graph.links or [])

    # "mgmt" is a LinkStatus, not a separate field. These are the shared OOB
    # bridge, where every device sees every other — real, but noise for
    # "what is leaf1 wired to", and on a 12-node fabric they outnumber the
    # real cabling several times over.
    if not include_mgmt:
        links = [ln for ln in links if ln.status != "mgmt"]

    def ep(dev_id, iface):
        return f"{names.get(dev_id, dev_id)}:{iface or '?'}"

    if device:
        needle = device.strip().lower()
        links = [
            ln
            for ln in links
            if needle == names.get(ln.source, "").lower()
            or needle == names.get(ln.target, "").lower()
        ]
        if not links:
            return (
                f"No fabric links found for {device!r}. Either the name is wrong "
                "(try list_devices), or LLDP has not been swept yet."
            )

    if not links:
        return (
            "The topology graph has no fabric links yet. Devices are imported but "
            "their LLDP neighbours have not been observed — this is normal for a "
            "few minutes after an import, or if the devices are unreachable."
        )

    lines = [
        f"{len(graph.nodes or [])} node(s), {len(links)} fabric link(s)"
        + (f" involving {device}" if device else "")
        + (" (management links excluded)" if not include_mgmt else ""),
        "",
        "endpoint_a <-> endpoint_b | status",
    ]
    for ln in links[:_MAX_ROWS]:
        lines.append(
            f"{ep(ln.source, ln.source_if)} <-> {ep(ln.target, ln.target_if)} | {ln.status}"
        )
    if len(links) > _MAX_ROWS:
        lines.append(f"\n(showing first {_MAX_ROWS} of {len(links)})")
    return "\n".join(lines)


LIST_DEVICES_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "platform": {
            "type": "string",
            "description": "Filter by platform, e.g. 'cumulus-linux' or 'linux'.",
        },
        "role": {
            "type": "string",
            "description": "Filter by role/tag, e.g. 'spine', 'leaf', 'server'.",
        },
        "name_contains": {
            "type": "string",
            "description": "Substring match on device name, e.g. 'leaf'.",
        },
        "environment": {
            "type": "string",
            "description": (
                "NV-CM environment slug to query. Omit for the tenant's "
                "default source of truth."
            ),
        },
    },
    "required": [],
}

GET_DEVICE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "device": {
            "type": "string",
            "description": "Device name (e.g. 'leaf1') or Nautobot UUID.",
        },
        "environment": {
            "type": "string",
            "description": "NV-CM environment slug. Omit for the default SoT.",
        },
    },
    "required": ["device"],
}

GET_TOPOLOGY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "device": {
            "type": "string",
            "description": (
                "Limit to links touching this device, e.g. 'leaf1'. "
                "Omit for the whole fabric."
            ),
        },
        "include_mgmt": {
            "type": "boolean",
            "description": (
                "Include out-of-band management links. Default false — the "
                "mgmt segment is a shared bridge where every device sees "
                "every other, which is real but rarely what is being asked."
            ),
        },
        "environment": {
            "type": "string",
            "description": "NV-CM environment slug. Omit for the default SoT.",
        },
    },
    "required": [],
}


def tool_specs() -> dict[str, dict[str, Any]]:
    """Return SoT tool specs in the same shape ``cloud_*.tool_specs()`` does.

    The kube_tools registry merges these in via ``_register_sot_tools()``
    so the alert-chat surface (``anthropic_tool_definitions``,
    ``execute_tool``, ``tool_requires_approval``) sees them as plain
    tools without special-casing.
    """
    return {
        "list_devices": {
            "description": (
                "List the devices in the tenant's source of truth — the "
                "real inventory, not a guess. Use this to answer any "
                "question about what infrastructure exists, how many "
                "devices there are, or what a device's IP or platform "
                "is. Optional filters: role (spine/leaf/server), "
                "platform, name_contains."
            ),
            "input_schema": LIST_DEVICES_INPUT_SCHEMA,
            "handler": _list_devices,
            "requires_approval": False,
        },
        "get_device": {
            "description": (
                "Full detail for one device by name (e.g. 'leaf1') or "
                "UUID: primary IP, platform, transport, tags and any "
                "custom fields. Use after list_devices when the user "
                "asks about a specific box."
            ),
            "input_schema": GET_DEVICE_INPUT_SCHEMA,
            "handler": _get_device,
            "requires_approval": False,
        },
        "get_fabric_topology": {
            "description": (
                "The cabling graph observed from live LLDP: which device "
                "port connects to which. Use for 'what is X connected "
                "to', 'how is the fabric wired', or to check a link's "
                "status. Pass device= to scope it to one box. This is "
                "observed reality, not the uploaded inventory."
            ),
            "input_schema": GET_TOPOLOGY_INPUT_SCHEMA,
            "handler": _get_fabric_topology,
            "requires_approval": False,
        },
        "propose_change": {
            "description": (
                "Author a ChangeProposal that captures the desired "
                "LinuxFacts state for a device, the reasoning behind "
                "the change, and the supporting evidence. The proposal "
                "lands in `pending` status; an operator approves it in "
                "the Proposals UI, and the executor service is the only "
                "thing that can then push it to the device. Use this "
                "whenever an alert investigation concludes that a "
                "managed device's config should be changed — never try "
                "to push directly via call_external_api or any other "
                "tool."
            ),
            "input_schema": PROPOSE_CHANGE_INPUT_SCHEMA,
            "handler": _propose_change,
            "requires_approval": False,
        },
    }
