"""Coverage for the Redfish device adapter (PR 5).

The adapter sits behind the same DeviceAdapter ABC as LinuxSSH, so the
tests follow the same shape: render produces a deterministic mapping,
diff detects per-fact changes, collect parses canned BMC responses,
execute issues the right HTTP calls in the right order.

Three integration assertions worth pinning beyond the adapter itself:

* ``NautobotSoT`` dispatches to ``_parse_redfish_facts`` when the
  device's transport is "redfish" (so a single shared SoT works
  uniformly for Linux servers and BMCs).
* ``change_proposals.resolve_credentials`` returns Redfish-shaped
  Credentials (port 443, no key, basic-auth password) for
  ``transport="redfish"``.
* ``register_device_adapter`` actually placed ``RedfishAdapter`` under
  the "redfish" slot — ``get_device_adapter("redfish")`` resolves.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers — canned Redfish responses + transport mocker
# ---------------------------------------------------------------------------


def _systems_index(system_id: str = "System.Embedded.1") -> dict:
    return {
        "Members": [
            {"@odata.id": f"/redfish/v1/Systems/{system_id}"},
        ]
    }


def _system_doc(
    system_id: str = "System.Embedded.1",
    *,
    power_state: str = "On",
    boot_target: str = "None",
    boot_enabled: str = "Disabled",
) -> dict:
    return {
        "Id": system_id,
        "PowerState": power_state,
        "Boot": {
            "BootSourceOverrideTarget": boot_target,
            "BootSourceOverrideEnabled": boot_enabled,
        },
    }


def _bios_doc(attrs: dict[str, object]) -> dict:
    return {"Attributes": attrs}


def _install_redfish_mock(
    monkeypatch, routes: dict[tuple[str, str], dict | None]
) -> dict[str, list[httpx.Request]]:
    """Mount an httpx.MockTransport into the Redfish adapter's client factory.

    routes: (method, path) → response body (dict) — None means 404.
    Returns a dict with a "requests" list so tests can assert on the
    requests the adapter actually made (e.g., verify the PATCH body).
    """
    seen: dict[str, list[httpx.Request]] = {"requests": []}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["requests"].append(req)
        key = (req.method, req.url.path)
        body = routes.get(key, "__missing__")
        if body == "__missing__":
            # Default: 200 with empty members so list_system_ids returns []
            # only when not overridden. For an unmatched route, this
            # surfaces as an explicit failure (we'd rather see the test
            # blow up than silently succeed).
            return httpx.Response(404, json={"error": f"unrouted {req.method} {req.url.path}"})
        if body is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)

    def _build(creds):
        return httpx.AsyncClient(
            base_url=f"https://{creds.host}",
            transport=transport,
            headers={"Accept": "application/json"},
        )

    monkeypatch.setattr(
        "daalu_automation.core.device.redfish._build_http_client", _build
    )
    return seen


# ---------------------------------------------------------------------------
# 1. Render — deterministic file mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_render_emits_three_files_when_all_facts_present():
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.sot.models import (
        BiosAttribute,
        BootOverride,
        PowerControl,
        RedfishFacts,
    )

    adapter = get_device_adapter("redfish")
    facts = RedfishFacts(
        bios_attributes=[
            BiosAttribute(name="BootMode", value="Uefi"),
            BiosAttribute(name="LogicalProc", value="Disabled"),
        ],
        boot_override=BootOverride(target="Pxe", enabled="Once"),
        power=PowerControl(desired_state="On"),
    )
    rendered = await adapter.render(facts)
    assert rendered.renderer_version == "redfish.v1"
    assert set(rendered.files) == {
        "/_redfish/power.json",
        "/_redfish/boot_override.json",
        "/_redfish/bios_attributes.json",
    }
    # Stable order (sorted keys) so the diff doesn't flicker.
    bios = json.loads(rendered.files["/_redfish/bios_attributes.json"])
    assert list(bios.keys()) == ["BootMode", "LogicalProc"]


@pytest.mark.asyncio(loop_scope="session")
async def test_render_skips_unmanaged_facts():
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.sot.models import RedfishFacts

    adapter = get_device_adapter("redfish")
    rendered = await adapter.render(RedfishFacts())
    # No facts set → no files
    assert rendered.files == {}


# ---------------------------------------------------------------------------
# 2. Diff — per-fact change detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_diff_detects_power_boot_and_bios_changes():
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.sot.models import (
        BiosAttribute,
        BootOverride,
        PowerControl,
        RedfishFacts,
    )

    adapter = get_device_adapter("redfish")
    observed = RedfishFacts(
        bios_attributes=[BiosAttribute(name="BootMode", value="Bios")],
        boot_override=BootOverride(target="None", enabled="Disabled"),
        power=PowerControl(desired_state="Off"),
    )
    intended = RedfishFacts(
        bios_attributes=[BiosAttribute(name="BootMode", value="Uefi")],
        boot_override=BootOverride(target="Pxe", enabled="Once"),
        power=PowerControl(desired_state="On"),
    )
    diff = await adapter.diff(observed, intended)
    assert diff.has_changes is True
    assert set(diff.facts_changed) == {
        "power.desired_state",
        "boot_override",
        "bios.BootMode",
    }
    assert "Uefi" in diff.unified_diff
    assert "Pxe" in diff.unified_diff


@pytest.mark.asyncio(loop_scope="session")
async def test_diff_empty_when_observed_matches_intended():
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.sot.models import PowerControl, RedfishFacts

    adapter = get_device_adapter("redfish")
    same = RedfishFacts(power=PowerControl(desired_state="On"))
    diff = await adapter.diff(same, same)
    assert diff.has_changes is False
    assert diff.facts_changed == []


# ---------------------------------------------------------------------------
# 3. Collect — parses canned BMC responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_collect_reads_power_boot_and_managed_bios(monkeypatch):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot.models import (
        BiosAttribute,
        RedfishFacts,
    )

    _install_redfish_mock(
        monkeypatch,
        {
            ("GET", "/redfish/v1/Systems/"): _systems_index(),
            ("GET", "/redfish/v1/Systems/System.Embedded.1"): _system_doc(
                power_state="On", boot_target="Pxe", boot_enabled="Once"
            ),
            ("GET", "/redfish/v1/Systems/System.Embedded.1/Bios"): _bios_doc(
                {"BootMode": "Uefi", "LogicalProc": "Enabled", "SriovGlobalEnable": "Enabled"}
            ),
        },
    )

    adapter = get_device_adapter("redfish")
    creds = Credentials(user="admin", host="10.0.0.50", port=443, password="x")
    hint = RedfishFacts(bios_attributes=[BiosAttribute(name="BootMode", value="?")])
    facts = await adapter.collect(creds, intended_hint=hint)

    assert isinstance(facts, RedfishFacts)
    assert facts.power.desired_state == "On"
    assert facts.boot_override.target == "Pxe"
    assert facts.boot_override.enabled == "Once"
    # Only the *managed* BIOS attr was collected — not every attr the
    # BMC reports.
    assert [a.name for a in facts.bios_attributes] == ["BootMode"]
    assert facts.bios_attributes[0].value == "Uefi"


@pytest.mark.asyncio(loop_scope="session")
async def test_collect_handles_bmc_with_no_bios_endpoint(monkeypatch):
    """Some BMCs (older iLOs, generic embedded) don't expose Bios via Redfish.

    Returning 404 there must NOT poison the rest of the collect — we
    still report power and boot facts.
    """
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot.models import BiosAttribute, RedfishFacts

    _install_redfish_mock(
        monkeypatch,
        {
            ("GET", "/redfish/v1/Systems/"): _systems_index(),
            ("GET", "/redfish/v1/Systems/System.Embedded.1"): _system_doc(),
            ("GET", "/redfish/v1/Systems/System.Embedded.1/Bios"): None,  # 404
        },
    )
    adapter = get_device_adapter("redfish")
    creds = Credentials(user="admin", host="10.0.0.50", port=443, password="x")
    facts = await adapter.collect(
        creds,
        intended_hint=RedfishFacts(bios_attributes=[BiosAttribute(name="X", value="?")]),
    )
    assert facts.power.desired_state == "On"
    assert facts.bios_attributes == []  # 404 → empty, not crash


# ---------------------------------------------------------------------------
# 4. Execute — issues the right HTTP calls in the right order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_execute_patches_bios_then_boot_then_power(monkeypatch):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot.models import (
        BiosAttribute,
        BootOverride,
        PowerControl,
        RedfishFacts,
    )

    seen = _install_redfish_mock(
        monkeypatch,
        {
            ("GET", "/redfish/v1/Systems/"): _systems_index(),
            # System currently Off → power.reset must fire (intent is On).
            ("GET", "/redfish/v1/Systems/System.Embedded.1"): _system_doc(power_state="Off"),
            ("PATCH", "/redfish/v1/Systems/System.Embedded.1/Bios/Settings"): {"ok": True},
            ("PATCH", "/redfish/v1/Systems/System.Embedded.1"): {"ok": True},
            ("POST", "/redfish/v1/Systems/System.Embedded.1/Actions/ComputerSystem.Reset"): {"ok": True},
        },
    )

    adapter = get_device_adapter("redfish")
    intended = RedfishFacts(
        bios_attributes=[BiosAttribute(name="BootMode", value="Uefi")],
        boot_override=BootOverride(target="Pxe", enabled="Once"),
        power=PowerControl(desired_state="On"),
    )
    rendered = await adapter.render(intended)

    creds = Credentials(user="admin", host="10.0.0.50", port=443, password="x")
    result = await adapter.execute(creds, rendered)
    assert result.success is True
    assert result.error == ""

    # The three write ops landed and in the documented order
    # (BIOS → boot → power), with the power-state check first.
    methods_paths = [(r.method, r.url.path) for r in seen["requests"]]
    bios_idx = methods_paths.index(
        ("PATCH", "/redfish/v1/Systems/System.Embedded.1/Bios/Settings")
    )
    boot_idx = methods_paths.index(("PATCH", "/redfish/v1/Systems/System.Embedded.1"))
    reset_idx = methods_paths.index(
        ("POST", "/redfish/v1/Systems/System.Embedded.1/Actions/ComputerSystem.Reset")
    )
    assert bios_idx < boot_idx < reset_idx

    # And per_step recorded each op
    ops = [s.get("op") for s in result.per_step]
    assert ops == ["bios.patch_settings", "boot.override", "power.reset"]


@pytest.mark.asyncio(loop_scope="session")
async def test_execute_skips_power_reset_when_state_already_matches(monkeypatch):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot.models import (
        PowerControl,
        RedfishFacts,
    )

    seen = _install_redfish_mock(
        monkeypatch,
        {
            ("GET", "/redfish/v1/Systems/"): _systems_index(),
            ("GET", "/redfish/v1/Systems/System.Embedded.1"): _system_doc(power_state="On"),
        },
    )
    adapter = get_device_adapter("redfish")
    rendered = await adapter.render(RedfishFacts(power=PowerControl(desired_state="On")))
    creds = Credentials(user="admin", host="10.0.0.50", port=443, password="x")
    result = await adapter.execute(creds, rendered)
    assert result.success is True
    # No POST to Actions/ComputerSystem.Reset
    methods = [(r.method, r.url.path) for r in seen["requests"]]
    assert not any(
        p.endswith("Actions/ComputerSystem.Reset") for m, p in methods if m == "POST"
    )
    # And per_step shows the skip with a reason
    assert any(
        s.get("op") == "power.reset" and s.get("skipped") is True
        for s in result.per_step
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_execute_failure_returns_error_without_raising(monkeypatch):
    """An HTTP error during a sub-step should surface in ExecutionResult,
    not as an exception that crashes the executor task."""
    import httpx

    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot.models import BiosAttribute, RedfishFacts

    def handler(req):
        if req.url.path == "/redfish/v1/Systems/":
            return httpx.Response(200, json=_systems_index())
        if req.method == "PATCH" and req.url.path.endswith("/Bios/Settings"):
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=_system_doc())

    monkeypatch.setattr(
        "daalu_automation.core.device.redfish._build_http_client",
        lambda creds: httpx.AsyncClient(
            base_url=f"https://{creds.host}",
            transport=httpx.MockTransport(handler),
        ),
    )

    adapter = get_device_adapter("redfish")
    rendered = await adapter.render(
        RedfishFacts(bios_attributes=[BiosAttribute(name="X", value="Y")])
    )
    creds = Credentials(user="admin", host="10.0.0.50", port=443, password="x")
    result = await adapter.execute(creds, rendered)
    assert result.success is False
    assert "boom" in result.error or "500" in result.error or "HTTPStatusError" in result.error
    # The BIOS step is the one that failed and is recorded as such.
    bios_step = next((s for s in result.per_step if s.get("op") == "bios.patch_settings"), None)
    assert bios_step is not None
    assert bios_step.get("ok") is False


# ---------------------------------------------------------------------------
# 5. Registry — adapter is discoverable
# ---------------------------------------------------------------------------


def test_redfish_adapter_registered():
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.redfish import RedfishAdapter

    a = get_device_adapter("redfish")
    assert isinstance(a, RedfishAdapter)
    assert a.transport == "redfish"


# ---------------------------------------------------------------------------
# 6. NautobotSoT dispatches Redfish parsing
# ---------------------------------------------------------------------------


def _nautobot_device_payload_redfish(device_id: str, *, intent: dict | None) -> dict:
    cfg = {"daalu_intent": intent} if intent is not None else {}
    return {
        "id": device_id,
        "name": "bmc01",
        "primary_ip4": {"address": "10.0.0.50/24"},
        "platform": {"name": "Dell iDRAC"},
        "tags": [],
        "config_context": cfg,
        # daalu_transport is omitted intentionally so the platform-name
        # fallback in _platform_bucket has to do the work.
        "custom_field_data": {},
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_nautobot_get_intended_returns_redfish_facts(
    db, seeded, fake_redis, nautobot_integration, mock_nautobot_transport,
):
    from daalu_automation.core.sot import NautobotSoT
    from daalu_automation.core.sot.models import RedfishFacts

    device_id = str(uuid.uuid4())
    redfish_intent = {
        "bios_attributes": [{"name": "BootMode", "value": "Uefi"}],
        "boot_override": {"target": "Pxe", "enabled": "Once"},
        "power": {"desired_state": "On"},
    }
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {
            "data": {"device": _nautobot_device_payload_redfish(device_id, intent=redfish_intent)}
        },
    })

    sot = NautobotSoT()
    intent = await sot.get_intended_config(db, seeded.tenant_a_id, device_id)
    assert intent is not None
    assert isinstance(intent.facts, RedfishFacts), (
        f"expected RedfishFacts, got {type(intent.facts).__name__}"
    )
    assert intent.facts.power.desired_state == "On"
    assert intent.facts.boot_override.target == "Pxe"
    assert intent.facts.bios_attributes[0].name == "BootMode"


# ---------------------------------------------------------------------------
# 7. resolve_credentials picks the redfish creds row for transport=redfish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_resolve_credentials_returns_redfish_creds(db, seeded):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.crypto import encrypt_secret
    from daalu_automation.core.sot.models import Device
    from daalu_automation.models import Integration, IntegrationStatus

    # Seed a redfish_credentials Integration row for tenant A.
    db.add(
        Integration(
            tenant_id=seeded.tenant_a_id,
            provider="redfish_credentials",
            module="infra",
            name="BMC creds for A",
            status=IntegrationStatus.connected,
            config={
                "user": "root",
                "password_ciphertext": encrypt_secret("calvin"),
                # verify_tls omitted → self-signed BMC certs OK by default
            },
        )
    )
    await db.commit()

    device = Device(
        id="dev-bmc-1",
        name="bmc01",
        primary_ip="10.0.0.50",
        platform="redfish",
        transport="redfish",
        tags=[],
        extra={},
    )
    creds = await cps.resolve_credentials(db, seeded.tenant_a_id, device)
    assert creds.user == "root"
    assert creds.host == "10.0.0.50"
    assert creds.port == 443
    assert creds.password == "calvin"
    assert creds.private_key_pem is None
    assert creds.sudo is False  # not meaningful for HTTP — keep False
    # known_hosts="verify" only when verify_tls=true was set; here None.
    assert creds.known_hosts is None


@pytest.mark.asyncio(loop_scope="session")
async def test_resolve_credentials_errors_with_no_redfish_row(db, seeded):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.sot.models import Device

    device = Device(
        id="dev-bmc-2",
        name="bmc02",
        primary_ip="10.0.0.51",
        platform="redfish",
        transport="redfish",
        tags=[],
    )
    with pytest.raises(LookupError, match="redfish_credentials"):
        await cps.resolve_credentials(db, seeded.tenant_a_id, device)
