"""Redfish device adapter — BMC / BIOS / boot order / power state.

Talks to a server's BMC (Dell iDRAC, HPE iLO, Lenovo XCC, generic
Redfish) over HTTPS using the DMTF DSP0266 surface:

* ``GET /redfish/v1/Systems/`` to enumerate
* ``GET /redfish/v1/Systems/{id}`` — power state + boot override
* ``GET /redfish/v1/Systems/{id}/Bios`` — current attribute set
* ``PATCH /redfish/v1/Systems/{id}/Bios/Settings`` — pending attribute
  changes (applied on next boot)
* ``PATCH /redfish/v1/Systems/{id}`` — set Boot.BootSourceOverride{Target,Enabled}
* ``POST /redfish/v1/Systems/{id}/Actions/ComputerSystem.Reset`` —
  power transitions

Three facts in scope for v1 (mirroring the narrowness of LinuxFacts):

* :class:`BiosAttribute` set — the adapter only manages keys you list
  in intent; unmanaged BIOS attrs are left untouched.
* :class:`BootOverride` — one-time / persistent boot device override.
* :class:`PowerControl` — desired stable power state (On/Off). The
  transition method (graceful vs force-reset) is a v1 simplification
  set on the adapter (``ForceRestart`` / ``ForceOff``).

Out of scope for v1: firmware updates via UpdateService.SimpleUpdate
(needs an image URL + multi-step task polling), virtual media mount,
SEL log ingestion. Each is a separate PR worth of work.
"""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timezone
from typing import Any, ClassVar

import httpx

from daalu_automation.core.device.base import DeviceAdapter
from daalu_automation.core.device.models import (
    ConfigDiff,
    Credentials,
    ExecutionResult,
    RenderedConfig,
)
from daalu_automation.core.device.registry import register_device_adapter
from daalu_automation.core.sot.models import (
    BiosAttribute,
    BootOverride,
    DeviceFacts,
    PowerControl,
    RedfishFacts,
)

logger = logging.getLogger(__name__)

RENDERER_VERSION = "redfish.v1"

# Rendered-config "file" paths — these aren't real files on disk; they
# are the labels under which the diff engine groups changes. The
# executor's stale-check compares these maps byte-for-byte.
PATH_POWER = "/_redfish/power.json"
PATH_BOOT = "/_redfish/boot_override.json"
PATH_BIOS = "/_redfish/bios_attributes.json"


# ── HTTP client factory (test seam) ──────────────────────────────────


def _build_http_client(creds: Credentials) -> httpx.AsyncClient:
    """Construct the AsyncClient used to talk to a BMC.

    Factored out as a module-level function so tests can monkeypatch it
    with one backed by ``httpx.MockTransport`` — same pattern as
    :mod:`daalu_automation.core.sot.nautobot`. Note that
    ``creds.port`` defaults to 22 in the model — the credentials
    resolver is expected to override to 443 for Redfish; we tolerate
    the fallback here only as a safety net for hand-built creds in
    tests.
    """
    port = creds.port if creds.port not in (0, 22) else 443
    base = f"https://{creds.host}:{port}"
    # verify_tls lives in extra (Credentials.model has no field for
    # it) — most BMCs ship with self-signed certs, so default off
    # unless the caller stuffed a sentinel in known_hosts.
    verify = bool(creds.known_hosts) if creds.known_hosts == "verify" else False
    auth = (creds.user, creds.password or "")
    return httpx.AsyncClient(
        base_url=base,
        timeout=30.0,
        verify=verify,
        auth=auth,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "OData-Version": "4.0",
        },
    )


# ── Render ────────────────────────────────────────────────────────────


def _render(facts: RedfishFacts) -> RenderedConfig:
    """Canonical text representation of the managed facts.

    JSON-per-file, sorted keys — gives a stable diff that doesn't
    flicker on dict ordering, and a stale-check that catches even
    whitespace-equivalent re-renders. The bias here is the same as
    :mod:`linux_ssh`: a renderer bump only invalidates proposals
    whose *content* actually changed, because the executor compares
    the files mapping byte-for-byte.
    """
    files: dict[str, str] = {}
    if facts.power is not None and facts.power.desired_state is not None:
        files[PATH_POWER] = (
            json.dumps({"desired_state": facts.power.desired_state}, sort_keys=True, indent=2)
            + "\n"
        )
    if facts.boot_override is not None:
        files[PATH_BOOT] = (
            json.dumps(facts.boot_override.model_dump(), sort_keys=True, indent=2) + "\n"
        )
    if facts.bios_attributes:
        attrs = {a.name: a.value for a in facts.bios_attributes}
        files[PATH_BIOS] = json.dumps(attrs, sort_keys=True, indent=2) + "\n"
    summary_parts: list[str] = []
    if facts.power and facts.power.desired_state:
        summary_parts.append(f"power={facts.power.desired_state}")
    if facts.boot_override:
        summary_parts.append(
            f"boot={facts.boot_override.target}/{facts.boot_override.enabled}"
        )
    if facts.bios_attributes:
        summary_parts.append(f"bios={len(facts.bios_attributes)} attrs")
    return RenderedConfig(
        renderer_version=RENDERER_VERSION,
        files=files,
        summary=" · ".join(summary_parts) or "(no managed facts)",
    )


# ── Diff ──────────────────────────────────────────────────────────────


def _diff(observed: RedfishFacts, intended: RedfishFacts) -> ConfigDiff:
    facts_changed: list[str] = []
    if intended.power is not None:
        obs_state = observed.power.desired_state if observed.power else None
        if intended.power.desired_state != obs_state:
            facts_changed.append("power.desired_state")
    if intended.boot_override is not None:
        if (
            observed.boot_override is None
            or observed.boot_override.target != intended.boot_override.target
            or observed.boot_override.enabled != intended.boot_override.enabled
        ):
            facts_changed.append("boot_override")
    if intended.bios_attributes:
        obs_map = {a.name: a.value for a in observed.bios_attributes}
        for attr in intended.bios_attributes:
            if obs_map.get(attr.name) != attr.value:
                facts_changed.append(f"bios.{attr.name}")
    if not facts_changed:
        return ConfigDiff(facts_changed=[], unified_diff="", has_changes=False)

    obs_render = _render(observed)
    int_render = _render(intended)
    lines: list[str] = []
    keys = sorted(set(obs_render.files) | set(int_render.files))
    for k in keys:
        a = obs_render.files.get(k, "")
        b = int_render.files.get(k, "")
        diff_iter = difflib.unified_diff(
            a.splitlines(keepends=True),
            b.splitlines(keepends=True),
            fromfile=f"a{k}",
            tofile=f"b{k}",
            n=3,
        )
        lines.extend(diff_iter)
    return ConfigDiff(
        facts_changed=facts_changed,
        unified_diff="".join(lines),
        has_changes=True,
    )


# ── HTTP helpers ──────────────────────────────────────────────────────


class RedfishUnavailable(RuntimeError):
    """BMC is unreachable or returned a fatal status."""


async def _list_system_ids(client: httpx.AsyncClient) -> list[str]:
    resp = await client.get("/redfish/v1/Systems/")
    resp.raise_for_status()
    members = (resp.json().get("Members") or [])
    # Members look like {"@odata.id": "/redfish/v1/Systems/System.Embedded.1"}
    return [m["@odata.id"].rstrip("/").rsplit("/", 1)[-1] for m in members if m.get("@odata.id")]


async def _get_system(client: httpx.AsyncClient, system_id: str) -> dict[str, Any]:
    resp = await client.get(f"/redfish/v1/Systems/{system_id}")
    resp.raise_for_status()
    return resp.json()


async def _get_bios(client: httpx.AsyncClient, system_id: str) -> dict[str, Any]:
    resp = await client.get(f"/redfish/v1/Systems/{system_id}/Bios")
    # 404 on Bios endpoint means the BMC doesn't expose BIOS via Redfish
    # — uncommon but not fatal; treat as "no BIOS attrs observed".
    if resp.status_code == 404:
        return {"Attributes": {}}
    resp.raise_for_status()
    return resp.json()


async def _patch_bios_settings(
    client: httpx.AsyncClient, system_id: str, attributes: dict[str, str]
) -> None:
    resp = await client.patch(
        f"/redfish/v1/Systems/{system_id}/Bios/Settings",
        json={"Attributes": attributes},
    )
    resp.raise_for_status()


async def _patch_boot(
    client: httpx.AsyncClient, system_id: str, boot: BootOverride
) -> None:
    resp = await client.patch(
        f"/redfish/v1/Systems/{system_id}",
        json={
            "Boot": {
                "BootSourceOverrideTarget": boot.target,
                "BootSourceOverrideEnabled": boot.enabled,
            }
        },
    )
    resp.raise_for_status()


async def _action_reset(
    client: httpx.AsyncClient, system_id: str, reset_type: str
) -> None:
    resp = await client.post(
        f"/redfish/v1/Systems/{system_id}/Actions/ComputerSystem.Reset",
        json={"ResetType": reset_type},
    )
    # Some BMCs return 202 (Accepted with a task); we don't poll the
    # task in v1 — the reset is fire-and-forget from the operator's
    # standpoint. 200 / 202 / 204 are all success.
    if resp.status_code not in (200, 202, 204):
        resp.raise_for_status()


# ── The adapter ───────────────────────────────────────────────────────


class RedfishAdapter(DeviceAdapter):
    transport: ClassVar[str] = "redfish"

    async def collect(
        self,
        creds: Credentials,
        intended_hint: DeviceFacts | None = None,
    ) -> RedfishFacts:
        # intended_hint is used only to scope BIOS-attribute observation
        # — we observe every key the operator manages, never the
        # device's full BIOS map (which can be hundreds of vendor
        # attrs). When no hint is provided we still collect power +
        # boot but leave bios_attributes empty.
        managed_bios_keys: list[str] = []
        if isinstance(intended_hint, RedfishFacts):
            managed_bios_keys = [a.name for a in intended_hint.bios_attributes]

        async with _build_http_client(creds) as client:
            system_ids = await _list_system_ids(client)
            if not system_ids:
                raise RedfishUnavailable(
                    f"BMC at {creds.host} returned no Systems members"
                )
            system_id = system_ids[0]  # Per-BMC multi-system is exotic
            sys_doc = await _get_system(client, system_id)
            facts = RedfishFacts()

            power_state = sys_doc.get("PowerState")
            if power_state in ("On", "Off"):
                facts.power = PowerControl(desired_state=power_state)

            boot = sys_doc.get("Boot") or {}
            target = boot.get("BootSourceOverrideTarget")
            enabled = boot.get("BootSourceOverrideEnabled")
            # Only populate if the BMC actually reports the override
            # state — we don't want to mis-attribute "no override
            # configured" as a managed fact.
            if target and enabled:
                try:
                    facts.boot_override = BootOverride(target=target, enabled=enabled)
                except Exception:
                    logger.warning(
                        "redfish.boot.invalid",
                        extra={"raw": boot, "host": creds.host},
                    )

            if managed_bios_keys:
                bios_doc = await _get_bios(client, system_id)
                attrs = bios_doc.get("Attributes") or {}
                for key in managed_bios_keys:
                    if key in attrs:
                        facts.bios_attributes.append(
                            BiosAttribute(name=key, value=str(attrs[key]))
                        )
        return facts

    async def render(self, intended: DeviceFacts) -> RenderedConfig:
        if not isinstance(intended, RedfishFacts):
            raise TypeError(
                f"RedfishAdapter.render expected RedfishFacts, got "
                f"{type(intended).__name__}"
            )
        return _render(intended)

    async def diff(
        self, observed: DeviceFacts, intended: DeviceFacts
    ) -> ConfigDiff:
        if not isinstance(observed, RedfishFacts) or not isinstance(intended, RedfishFacts):
            raise TypeError(
                "RedfishAdapter.diff requires both observed and intended "
                "to be RedfishFacts"
            )
        return _diff(observed, intended)

    async def execute(
        self, creds: Credentials, rendered: RenderedConfig
    ) -> ExecutionResult:
        """Apply the rendered facts to the BMC.

        Order of operations:

        1. BIOS attribute patches (queued for next boot — does *not*
           reboot by itself; the operator gets a separate proposal if
           a reboot is needed).
        2. Boot override patch.
        3. Power transition (last — anything that needed a power state
           change is the operator's explicit ask, and we want the
           other settings in place when the new state takes effect).

        Returns a per-step trace so the operator can see which sub-call
        succeeded vs failed. Rollback is not attempted: Redfish actions
        are mostly idempotent (PATCH with the original value un-does a
        change) and partial state is generally safe to re-converge by
        re-running.
        """
        started = datetime.now(tz=timezone.utc)
        # Re-parse the rendered JSON back into RedfishFacts so we
        # actually push the snapshot the operator approved, not whatever
        # current intent happens to be at execute time. The change_
        # proposals.execute() stale-check already guarantees this
        # rendered config IS the approved snapshot.
        intent = _facts_from_rendered(rendered)
        per_step: list[dict[str, Any]] = []
        try:
            async with _build_http_client(creds) as client:
                system_ids = await _list_system_ids(client)
                if not system_ids:
                    raise RedfishUnavailable(
                        f"BMC at {creds.host} returned no Systems members"
                    )
                system_id = system_ids[0]

                if intent.bios_attributes:
                    attrs = {a.name: a.value for a in intent.bios_attributes}
                    try:
                        await _patch_bios_settings(client, system_id, attrs)
                        per_step.append(
                            {"op": "bios.patch_settings", "attrs": list(attrs.keys()), "ok": True}
                        )
                    except httpx.HTTPError as e:
                        per_step.append(
                            {"op": "bios.patch_settings", "ok": False, "error": str(e)}
                        )
                        raise

                if intent.boot_override is not None:
                    try:
                        await _patch_boot(client, system_id, intent.boot_override)
                        per_step.append({
                            "op": "boot.override",
                            "target": intent.boot_override.target,
                            "enabled": intent.boot_override.enabled,
                            "ok": True,
                        })
                    except httpx.HTTPError as e:
                        per_step.append(
                            {"op": "boot.override", "ok": False, "error": str(e)}
                        )
                        raise

                if intent.power is not None and intent.power.desired_state is not None:
                    # Observe first so we only fire ResetType if it
                    # would actually flip state. Idempotent transitions
                    # save the operator from accidental reboots when
                    # re-running a stuck proposal.
                    cur = await _get_system(client, system_id)
                    if cur.get("PowerState") != intent.power.desired_state:
                        reset_type = (
                            "On" if intent.power.desired_state == "On" else "ForceOff"
                        )
                        try:
                            await _action_reset(client, system_id, reset_type)
                            per_step.append(
                                {"op": "power.reset", "reset_type": reset_type, "ok": True}
                            )
                        except httpx.HTTPError as e:
                            per_step.append(
                                {"op": "power.reset", "ok": False, "error": str(e)}
                            )
                            raise
                    else:
                        per_step.append(
                            {"op": "power.reset", "skipped": True, "reason": "already in desired state"}
                        )
        except Exception as e:  # noqa: BLE001
            return ExecutionResult(
                success=False,
                started_at=started.isoformat(),
                finished_at=datetime.now(tz=timezone.utc).isoformat(),
                per_step=per_step,
                rollback_performed=False,
                error=f"{type(e).__name__}: {e}",
            )

        return ExecutionResult(
            success=True,
            started_at=started.isoformat(),
            finished_at=datetime.now(tz=timezone.utc).isoformat(),
            per_step=per_step,
            rollback_performed=False,
            error="",
        )


def _facts_from_rendered(rendered: RenderedConfig) -> RedfishFacts:
    facts = RedfishFacts()
    if PATH_POWER in rendered.files:
        try:
            data = json.loads(rendered.files[PATH_POWER])
            if data.get("desired_state") in ("On", "Off"):
                facts.power = PowerControl(desired_state=data["desired_state"])
        except json.JSONDecodeError:
            logger.warning("redfish.rendered.power.bad_json")
    if PATH_BOOT in rendered.files:
        try:
            data = json.loads(rendered.files[PATH_BOOT])
            facts.boot_override = BootOverride(**data)
        except Exception:
            logger.warning("redfish.rendered.boot.bad_json")
    if PATH_BIOS in rendered.files:
        try:
            data = json.loads(rendered.files[PATH_BIOS])
            for k, v in data.items():
                facts.bios_attributes.append(BiosAttribute(name=k, value=str(v)))
        except Exception:
            logger.warning("redfish.rendered.bios.bad_json")
    return facts


# Side-effect: register under transport="redfish" so
# get_device_adapter("redfish") resolves once this module is imported.
register_device_adapter(RedfishAdapter)
