"""Coverage for the Junos / IOS-XR / Arista EOS device adapters (PR 6).

All three adapters share the same render/diff pipeline (via
``_net_common``) and a near-identical execute sequence, so each
section uses parametrization where it can and pins per-adapter
specifics where the vendor surface differs (XML namespace for NETCONF,
CLI line shape for EOS, commit-confirmed → confirm vs. write-memory).

Integration assertions worth pinning beyond the adapter internals:

* ``NautobotSoT`` dispatches to ``_parse_network_facts`` for a device
  whose transport is ``junos`` (representative test — the dispatch
  logic is identical for ``iosxr`` and ``eos``).
* ``change_proposals.resolve_credentials`` returns network-creds
  shaped Credentials (port 22, no key required, optional
  enable_password) for a ``junos`` device using a
  ``network_credentials`` Integration row.
* The PR-9 per-device credentials priority resolver works for the
  new ``network_credentials`` provider: a per-device row beats the
  tenant-wide fallback.
* ``register_device_adapter`` placed all three under
  ``get_device_adapter("junos" / "iosxr" / "eos")``.
"""

from __future__ import annotations

import json
import uuid

import pytest

# ---------------------------------------------------------------------------
# Fake transport drivers — one for NETCONF, one for CLI
# ---------------------------------------------------------------------------


class _FakeNetconfDriver:
    """Records every NETCONF method invocation for assertion.

    The two NETCONF adapters (Junos, IOS-XR) call ``open``,
    ``edit_config``, ``rpc``, ``get_config``, ``close`` — that's the
    full surface this fake needs.
    """

    def __init__(self, get_config_xml: str = "<configuration/>") -> None:
        self.calls: list[tuple[str, dict]] = []
        self._get_config_xml = get_config_xml
        self._fail_on: str | None = None
        self._fail_exc: Exception | None = None

    def fail_on(self, op: str, exc: Exception) -> None:
        self._fail_on = op
        self._fail_exc = exc

    async def open(self) -> None:
        self.calls.append(("open", {}))

    async def get_config(self, *, source: str = "running", filter_: str = "") -> str:
        self.calls.append(("get_config", {"source": source, "filter_": filter_}))
        if self._fail_on == "get_config":
            raise self._fail_exc  # type: ignore[misc]
        return self._get_config_xml

    async def edit_config(self, *, config: str, target: str = "candidate") -> str:
        self.calls.append(("edit_config", {"config": config, "target": target}))
        if self._fail_on == "edit_config":
            raise self._fail_exc  # type: ignore[misc]
        return "<ok/>"

    async def rpc(self, *, filter_: str = "") -> str:
        self.calls.append(("rpc", {"filter_": filter_}))
        if self._fail_on == "rpc" or (
            self._fail_on == "commit_confirmed"
            and "confirmed" in filter_
        ):
            raise self._fail_exc  # type: ignore[misc]
        return "<ok/>"

    async def close(self) -> None:
        self.calls.append(("close", {}))


class _FakeCliDriver:
    """Records every Scrapli-CLI method invocation for assertion."""

    def __init__(self, show_running: str = "") -> None:
        self.calls: list[tuple[str, object]] = []
        self._show_running = show_running
        self._fail_on: str | None = None
        self._fail_exc: Exception | None = None

    def fail_on(self, op: str, exc: Exception) -> None:
        self._fail_on = op
        self._fail_exc = exc

    async def open(self) -> None:
        self.calls.append(("open", None))

    async def send_command(self, command: str) -> str:
        self.calls.append(("send_command", command))
        if self._fail_on == "send_command":
            raise self._fail_exc  # type: ignore[misc]
        if command.startswith("show running"):
            return self._show_running
        return ""

    async def send_configs(self, configs: list[str]) -> str:
        self.calls.append(("send_configs", list(configs)))
        if self._fail_on == "send_configs":
            raise self._fail_exc  # type: ignore[misc]
        return ""

    async def close(self) -> None:
        self.calls.append(("close", None))


def _install_netconf_fake(monkeypatch, transport_module_path: str, fake: _FakeNetconfDriver) -> None:
    async def _opener(creds):
        await fake.open()
        return fake

    monkeypatch.setattr(f"{transport_module_path}._open_netconf", _opener)
    # Zero out the post-commit settle so the test doesn't sleep a second.
    monkeypatch.setattr(f"{transport_module_path}._SETTLE_SECONDS", 0)


def _install_cli_fake(monkeypatch, fake: _FakeCliDriver) -> None:
    async def _opener(creds):
        await fake.open()
        return fake

    monkeypatch.setattr("daalu_automation.core.device.eos._open_cli", _opener)


# ---------------------------------------------------------------------------
# 1. Render — deterministic per-file mapping (shared body, per-adapter version)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transport", "expected_version"),
    [("junos", "junos.v1"), ("iosxr", "iosxr.v1"), ("eos", "eos.v1")],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_render_emits_per_file_layout(transport, expected_version):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.sot.models import (
        InterfaceConfig,
        NetworkFacts,
        StaticRoute,
        VlanDefinition,
    )

    adapter = get_device_adapter(transport)
    facts = NetworkFacts(
        hostname="leaf01",
        interfaces=[
            InterfaceConfig(
                name="Ethernet1", description="uplink", ipv4_address="10.0.0.1/24"
            ),
            InterfaceConfig(name="Ethernet2", enabled=False),
        ],
        vlans=[
            VlanDefinition(vlan_id=100, name="users"),
            VlanDefinition(vlan_id=200, name="mgmt"),
        ],
        static_routes=[
            StaticRoute(prefix="0.0.0.0/0", next_hop="10.0.0.254"),
        ],
    )
    rendered = await adapter.render(facts)
    assert rendered.renderer_version == expected_version
    # One file per interface + hostname + vlans + routes (4 files total).
    assert set(rendered.files) == {
        "/_net/hostname.json",
        "/_net/interfaces/Ethernet1.json",
        "/_net/interfaces/Ethernet2.json",
        "/_net/vlans.json",
        "/_net/static_routes.json",
    }
    # vlans sorted by id, so re-render is stable
    vlans = json.loads(rendered.files["/_net/vlans.json"])
    assert [v["vlan_id"] for v in vlans["vlans"]] == [100, 200]


@pytest.mark.parametrize("transport", ["junos", "iosxr", "eos"])
@pytest.mark.asyncio(loop_scope="session")
async def test_render_empty_when_no_managed_facts(transport):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.sot.models import NetworkFacts

    adapter = get_device_adapter(transport)
    rendered = await adapter.render(NetworkFacts())
    assert rendered.files == {}


# ---------------------------------------------------------------------------
# 2. Diff — per-fact change detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transport", ["junos", "iosxr", "eos"])
@pytest.mark.asyncio(loop_scope="session")
async def test_diff_detects_per_fact_changes(transport):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.sot.models import (
        InterfaceConfig,
        NetworkFacts,
        StaticRoute,
        VlanDefinition,
    )

    adapter = get_device_adapter(transport)
    observed = NetworkFacts(
        hostname="leaf01-old",
        interfaces=[InterfaceConfig(name="Ethernet1", description="old")],
        vlans=[],
        static_routes=[],
    )
    intended = NetworkFacts(
        hostname="leaf01",
        interfaces=[InterfaceConfig(name="Ethernet1", description="new")],
        vlans=[VlanDefinition(vlan_id=100, name="users")],
        static_routes=[StaticRoute(prefix="10.0.0.0/24", next_hop="10.0.0.254")],
    )
    diff = await adapter.diff(observed, intended)
    assert diff.has_changes is True
    assert set(diff.facts_changed) == {
        "hostname",
        "interface.Ethernet1",
        "vlan.100",
        "route.10.0.0.0/24",
    }
    assert "leaf01" in diff.unified_diff
    assert "10.0.0.0/24" in diff.unified_diff


@pytest.mark.parametrize("transport", ["junos", "iosxr", "eos"])
@pytest.mark.asyncio(loop_scope="session")
async def test_diff_empty_when_observed_matches_intended(transport):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.sot.models import (
        InterfaceConfig,
        NetworkFacts,
    )

    adapter = get_device_adapter(transport)
    same = NetworkFacts(
        hostname="leaf01",
        interfaces=[InterfaceConfig(name="Ethernet1", description="x")],
    )
    diff = await adapter.diff(same, same)
    assert diff.has_changes is False
    assert diff.facts_changed == []


# ---------------------------------------------------------------------------
# 3. Collect — parses canned NETCONF / CLI responses, scoped to hint
# ---------------------------------------------------------------------------


_JUNOS_GET_CONFIG_XML = """
<rpc-reply>
  <data>
    <configuration>
      <system><host-name>leaf01</host-name></system>
      <interfaces>
        <interface><name>ge-0/0/0</name>
          <description>uplink</description>
          <unit><name>0</name>
            <family><inet><address><name>10.0.0.1/24</name></address></inet></family>
          </unit>
        </interface>
        <interface><name>ge-0/0/1</name>
          <disable/>
        </interface>
      </interfaces>
      <vlans>
        <vlan><name>users</name><vlan-id>100</vlan-id></vlan>
      </vlans>
      <routing-options>
        <static>
          <route><name>0.0.0.0/0</name><next-hop>10.0.0.254</next-hop></route>
        </static>
      </routing-options>
    </configuration>
  </data>
</rpc-reply>
"""


@pytest.mark.asyncio(loop_scope="session")
async def test_junos_collect_parses_netconf_and_scopes_interfaces(monkeypatch):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot.models import (
        InterfaceConfig,
        NetworkFacts,
    )

    fake = _FakeNetconfDriver(get_config_xml=_JUNOS_GET_CONFIG_XML)
    _install_netconf_fake(monkeypatch, "daalu_automation.core.device.junos", fake)

    adapter = get_device_adapter("junos")
    creds = Credentials(user="admin", host="10.0.0.50", port=830, password="x")
    # Only ge-0/0/0 in the hint → only that one observed.
    hint = NetworkFacts(interfaces=[InterfaceConfig(name="ge-0/0/0")])
    facts = await adapter.collect(creds, intended_hint=hint)

    assert facts.hostname == "leaf01"
    assert [i.name for i in facts.interfaces] == ["ge-0/0/0"]
    assert facts.interfaces[0].ipv4_address == "10.0.0.1/24"
    assert facts.interfaces[0].description == "uplink"
    # VLANs + routes were enumerated (small enough to fetch).
    assert [v.vlan_id for v in facts.vlans] == [100]
    assert facts.static_routes[0].prefix == "0.0.0.0/0"
    # And the get_config filter scoped interfaces to the hint name.
    get_call = next(c for c in fake.calls if c[0] == "get_config")
    assert "ge-0/0/0" in get_call[1]["filter_"]
    assert "ge-0/0/1" not in get_call[1]["filter_"]  # not in hint → not requested


_IOSXR_GET_CONFIG_XML = """
<rpc-reply>
  <data>
    <host-names xmlns="http://cisco.com/ns/yang/Cisco-IOS-XR-shellutil-cfg">
      <host-name>xr01</host-name>
    </host-names>
    <interface-configurations xmlns="http://cisco.com/ns/yang/Cisco-IOS-XR-ifmgr-cfg">
      <interface-configuration>
        <interface-name>GigabitEthernet0/0/0/0</interface-name>
        <description>core uplink</description>
        <ipv4-network xmlns="http://cisco.com/ns/yang/Cisco-IOS-XR-ipv4-io-cfg">
          <addresses><primary>
            <address>192.0.2.1</address>
            <netmask>255.255.255.0</netmask>
          </primary></addresses>
        </ipv4-network>
      </interface-configuration>
    </interface-configurations>
    <router-static xmlns="http://cisco.com/ns/yang/Cisco-IOS-XR-ip-static-cfg">
      <default-vrf><address-family><vrfipv4><vrf-unicast><vrf-prefixes>
        <vrf-prefix>
          <prefix>0.0.0.0</prefix>
          <prefix-length>0</prefix-length>
          <vrf-route><vrf-next-hop-table><vrf-next-hop-next-hop-address>
            <next-hop-address>192.0.2.254</next-hop-address>
          </vrf-next-hop-next-hop-address></vrf-next-hop-table></vrf-route>
        </vrf-prefix>
      </vrf-prefixes></vrf-unicast></vrfipv4></address-family></default-vrf>
    </router-static>
  </data>
</rpc-reply>
"""


@pytest.mark.asyncio(loop_scope="session")
async def test_iosxr_collect_parses_netconf(monkeypatch):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot.models import InterfaceConfig, NetworkFacts

    fake = _FakeNetconfDriver(get_config_xml=_IOSXR_GET_CONFIG_XML)
    _install_netconf_fake(monkeypatch, "daalu_automation.core.device.iosxr", fake)

    adapter = get_device_adapter("iosxr")
    creds = Credentials(user="admin", host="10.0.0.51", port=830, password="x")
    hint = NetworkFacts(interfaces=[InterfaceConfig(name="GigabitEthernet0/0/0/0")])
    facts = await adapter.collect(creds, intended_hint=hint)

    assert facts.hostname == "xr01"
    assert facts.interfaces[0].description == "core uplink"
    assert facts.interfaces[0].ipv4_address == "192.0.2.1/24"
    assert facts.static_routes[0].prefix == "0.0.0.0/0"
    assert facts.static_routes[0].next_hop == "192.0.2.254"


_EOS_SHOW_RUNNING = """\
! some banner
!
hostname leaf-eos
!
vlan 100
   name users
!
vlan 200
   name mgmt
!
interface Ethernet1
   description uplink
   no switchport
   ip address 10.0.0.1/24
   no shutdown
!
interface Ethernet2
   description spare
   shutdown
!
ip route 0.0.0.0/0 10.0.0.254
ip route vrf customer-a 192.0.2.0/24 192.0.2.1
!
end
"""


@pytest.mark.asyncio(loop_scope="session")
async def test_eos_collect_parses_show_running_and_scopes_interfaces(monkeypatch):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot.models import InterfaceConfig, NetworkFacts

    fake = _FakeCliDriver(show_running=_EOS_SHOW_RUNNING)
    _install_cli_fake(monkeypatch, fake)

    adapter = get_device_adapter("eos")
    creds = Credentials(user="admin", host="10.0.0.52", port=22, password="x")
    hint = NetworkFacts(interfaces=[InterfaceConfig(name="Ethernet1")])
    facts = await adapter.collect(creds, intended_hint=hint)

    assert facts.hostname == "leaf-eos"
    # Hint only requested Ethernet1 — Ethernet2 NOT in collected facts.
    assert [i.name for i in facts.interfaces] == ["Ethernet1"]
    assert facts.interfaces[0].description == "uplink"
    assert facts.interfaces[0].ipv4_address == "10.0.0.1/24"
    # VLANs + routes still fully enumerated.
    assert sorted(v.vlan_id for v in facts.vlans) == [100, 200]
    assert any(r.vrf == "customer-a" for r in facts.static_routes)
    # And it issued exactly one `show running-config` call.
    show_calls = [c for c in fake.calls if c[0] == "send_command"]
    assert any("running-config" in c[1] for c in show_calls)


# ---------------------------------------------------------------------------
# 4. Execute — happy path, sequence pinned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transport,module_path",
    [
        ("junos", "daalu_automation.core.device.junos"),
        ("iosxr", "daalu_automation.core.device.iosxr"),
    ],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_netconf_execute_runs_commit_confirmed_then_commit(
    transport, module_path, monkeypatch
):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot.models import InterfaceConfig, NetworkFacts

    fake = _FakeNetconfDriver()
    _install_netconf_fake(monkeypatch, module_path, fake)

    adapter = get_device_adapter(transport)
    intent = NetworkFacts(
        hostname="leaf01",
        interfaces=[InterfaceConfig(name="ge-0/0/0", description="up")],
    )
    rendered = await adapter.render(intent)
    creds = Credentials(user="admin", host="10.0.0.50", port=830, password="x")
    result = await adapter.execute(creds, rendered)

    assert result.success is True
    # Exact sequence of NETCONF ops issued: open → edit_config → rpc(commit_confirmed)
    # → rpc(commit) → close.
    op_seq = [c[0] for c in fake.calls]
    assert op_seq == ["open", "edit_config", "rpc", "rpc", "close"]
    # First rpc carries "confirmed", second one is a plain commit.
    first_rpc = fake.calls[2][1]["filter_"]
    second_rpc = fake.calls[3][1]["filter_"]
    assert "confirmed" in first_rpc
    assert "600" in first_rpc  # default commit_confirmed_timeout_s
    assert "confirmed" not in second_rpc

    # Per-step trace matches the public ops.
    op_names = [s.get("op") for s in result.per_step]
    assert op_names == [
        "netconf.open",
        "edit_config",
        "commit_confirmed",
        "commit",
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_eos_execute_pushes_configs_then_saves_startup(monkeypatch):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot.models import (
        InterfaceConfig,
        NetworkFacts,
        VlanDefinition,
    )

    fake = _FakeCliDriver()
    _install_cli_fake(monkeypatch, fake)

    adapter = get_device_adapter("eos")
    intent = NetworkFacts(
        hostname="leaf-eos",
        vlans=[VlanDefinition(vlan_id=100, name="users")],
        interfaces=[InterfaceConfig(name="Ethernet1", description="uplink")],
    )
    rendered = await adapter.render(intent)
    creds = Credentials(user="admin", host="10.0.0.52", port=22, password="x")
    result = await adapter.execute(creds, rendered)

    assert result.success is True
    # open → send_configs(...) → send_command(copy run start) → close
    op_seq = [c[0] for c in fake.calls]
    assert op_seq == ["open", "send_configs", "send_command", "close"]
    pushed_lines = fake.calls[1][1]  # second call payload (list of lines)
    # The line builder emits hostname → vlans → interfaces, in that order.
    assert pushed_lines[0] == "hostname leaf-eos"
    assert any("vlan 100" in line for line in pushed_lines)
    assert any("interface Ethernet1" in line for line in pushed_lines)
    save_cmd = fake.calls[2][1]
    assert "copy running-config startup-config" in save_cmd

    op_names = [s.get("op") for s in result.per_step]
    assert op_names == ["cli.open", "send_configs", "save_startup"]


# ---------------------------------------------------------------------------
# 5. Execute — failure surfaces in ExecutionResult, not as a crash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transport,module_path",
    [
        ("junos", "daalu_automation.core.device.junos"),
        ("iosxr", "daalu_automation.core.device.iosxr"),
    ],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_netconf_execute_failure_notes_commit_confirmed_rollback(
    transport, module_path, monkeypatch
):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot.models import NetworkFacts

    fake = _FakeNetconfDriver()
    fake.fail_on("edit_config", RuntimeError("device refused"))
    _install_netconf_fake(monkeypatch, module_path, fake)

    adapter = get_device_adapter(transport)
    rendered = await adapter.render(NetworkFacts(hostname="leaf01"))
    creds = Credentials(user="admin", host="10.0.0.50", port=830, password="x")
    result = await adapter.execute(creds, rendered)

    assert result.success is False
    assert "device refused" in result.error
    # The per_step trace ends with a note pointing at the commit-
    # confirmed auto-rollback — we don't issue an explicit
    # <discard-changes/>, we rely on the device's own timer.
    note_steps = [s for s in result.per_step if "rollback" in (s.get("op") or "")]
    assert note_steps, f"expected a rollback note in per_step, got {result.per_step}"
    assert "auto-rollback" in note_steps[0].get("note", "")


@pytest.mark.asyncio(loop_scope="session")
async def test_eos_execute_failure_returns_error_without_raising(monkeypatch):
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.device.models import Credentials
    from daalu_automation.core.sot.models import NetworkFacts

    fake = _FakeCliDriver()
    fake.fail_on("send_configs", RuntimeError("config rejected"))
    _install_cli_fake(monkeypatch, fake)

    adapter = get_device_adapter("eos")
    rendered = await adapter.render(NetworkFacts(hostname="leaf-eos"))
    creds = Credentials(user="admin", host="10.0.0.52", port=22, password="x")
    result = await adapter.execute(creds, rendered)
    assert result.success is False
    assert "config rejected" in result.error


# ---------------------------------------------------------------------------
# 6. Registry — all three adapters discoverable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transport,cls_name",
    [
        ("junos", "JunosAdapter"),
        ("iosxr", "IOSXRAdapter"),
        ("eos", "EOSAdapter"),
    ],
)
def test_network_adapters_registered(transport, cls_name):
    from daalu_automation.core.device import get_device_adapter

    a = get_device_adapter(transport)
    assert a.__class__.__name__ == cls_name
    assert a.transport == transport


# ---------------------------------------------------------------------------
# 7. NautobotSoT dispatches network-facts parsing (junos covers the family)
# ---------------------------------------------------------------------------


def _nautobot_device_payload_junos(device_id: str, *, intent: dict | None) -> dict:
    cfg = {"daalu_intent": intent} if intent is not None else {}
    return {
        "id": device_id,
        "name": "leaf01",
        "primary_ip4": {"address": "10.0.0.50/24"},
        "platform": {"name": "Juniper Junos"},
        "tags": [],
        "config_context": cfg,
        "custom_field_data": {},  # platform-name fallback decides transport
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_nautobot_dispatches_network_facts_for_junos(
    db, seeded, fake_redis, nautobot_integration, mock_nautobot_transport,
):
    from daalu_automation.core.sot import NautobotSoT
    from daalu_automation.core.sot.models import NetworkFacts

    device_id = str(uuid.uuid4())
    network_intent = {
        "hostname": "leaf01",
        "interfaces": [
            {"name": "ge-0/0/0", "description": "uplink", "ipv4_address": "10.0.0.1/24"}
        ],
        "vlans": [{"vlan_id": 100, "name": "users"}],
        "static_routes": [{"prefix": "0.0.0.0/0", "next_hop": "10.0.0.254"}],
    }
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {
            "data": {
                "device": _nautobot_device_payload_junos(device_id, intent=network_intent)
            }
        },
    })

    sot = NautobotSoT()
    intent = await sot.get_intended_config(db, seeded.tenant_a_id, device_id)
    assert intent is not None
    assert isinstance(intent.facts, NetworkFacts)
    assert intent.facts.hostname == "leaf01"
    assert intent.facts.interfaces[0].name == "ge-0/0/0"
    assert intent.facts.vlans[0].vlan_id == 100
    assert intent.facts.static_routes[0].next_hop == "10.0.0.254"


# ---------------------------------------------------------------------------
# 8. resolve_credentials — network_credentials provider + enable_password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_resolve_credentials_returns_network_creds_with_enable(db, seeded):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.crypto import encrypt_secret
    from daalu_automation.core.sot.models import Device
    from daalu_automation.models import Integration, IntegrationStatus

    db.add(
        Integration(
            tenant_id=seeded.tenant_a_id,
            provider="network_credentials",
            module="infra",
            name="Network creds for A",
            status=IntegrationStatus.connected,
            config={
                "user": "netops",
                "password_ciphertext": encrypt_secret("secret-1"),
                "enable_password_ciphertext": encrypt_secret("enable-secret"),
            },
        )
    )
    await db.commit()

    device = Device(
        id="dev-junos-1",
        name="leaf01",
        primary_ip="10.0.0.50",
        platform="junos",
        transport="junos",
        tags=[],
        extra={},
    )
    creds = await cps.resolve_credentials(db, seeded.tenant_a_id, device)
    assert creds.user == "netops"
    assert creds.host == "10.0.0.50"
    assert creds.port == 22  # default; NETCONF adapter bumps to 830 internally
    assert creds.password == "secret-1"
    assert creds.enable_password == "enable-secret"
    assert creds.private_key_pem is None
    assert creds.sudo is False


@pytest.mark.asyncio(loop_scope="session")
async def test_resolve_credentials_errors_with_no_network_row(db, seeded):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.sot.models import Device

    device = Device(
        id="dev-iosxr-2",
        name="xr01",
        primary_ip="10.0.0.51",
        platform="iosxr",
        transport="iosxr",
        tags=[],
    )
    with pytest.raises(LookupError, match="network_credentials"):
        await cps.resolve_credentials(db, seeded.tenant_a_id, device)


# ---------------------------------------------------------------------------
# 9. PR-9 priority: per-device row wins over tenant-wide for network_credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_resolve_credentials_per_device_row_wins(db, seeded):
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.crypto import encrypt_secret
    from daalu_automation.core.sot.models import Device
    from daalu_automation.models import Integration, IntegrationStatus

    # Tenant-wide fallback row.
    db.add(
        Integration(
            tenant_id=seeded.tenant_a_id,
            provider="network_credentials",
            module="infra",
            name="fleet default",
            status=IntegrationStatus.connected,
            config={
                "user": "fleet",
                "password_ciphertext": encrypt_secret("fleet-pw"),
            },
        )
    )
    # Per-device override targeting dev-xr-special.
    db.add(
        Integration(
            tenant_id=seeded.tenant_a_id,
            provider="network_credentials",
            module="infra",
            name="dev-xr-special override",
            status=IntegrationStatus.connected,
            config={
                "device_id": "dev-xr-special",
                "user": "vip",
                "password_ciphertext": encrypt_secret("vip-pw"),
            },
        )
    )
    await db.commit()

    device = Device(
        id="dev-xr-special",
        name="xr-vip",
        primary_ip="10.0.0.99",
        platform="iosxr",
        transport="iosxr",
        tags=[],
    )
    creds = await cps.resolve_credentials(db, seeded.tenant_a_id, device)
    assert creds.user == "vip"
    assert creds.password == "vip-pw"
