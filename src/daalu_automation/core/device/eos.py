"""Arista EOS device adapter (Scrapli CLI over SSH).

Unlike Junos / IOS-XR, Arista EOS has no native NETCONF
commit-confirmed surface (the eAPI rollback story exists but isn't
parity with NETCONF). For v1 we lean on:

* Scrapli's ``arista_eos`` platform driver for SSH-CLI access.
* ``configure session <name>`` blocks if the device supports them,
  otherwise fall back to standalone config-mode pushes.
* ``write memory`` to persist running-config → startup-config so a
  later reload doesn't lose the change.

**No native rollback is performed by this adapter** (v1 simplification
decided up-front — see PR 6 spec). The blast-radius story for EOS
relies on:

1. The narrow ``NetworkFacts`` shape — the rendered diff shows
   exactly which interface / VLAN / route changes, and the operator
   reads it before approving the proposal.
2. The ``ChangeProposal`` evidence carrying the observed pre-state,
   so a manual revert is one re-render away.
3. A future PR can layer a ``reload in 5 / reload cancel`` belt-and-
   braces scheme on top without changing this contract.

If you reach for this module looking for `reload in N` — that's
deliberately not here. Open an issue if your environment needs it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, ClassVar

from daalu_automation.core.device import _net_common
from daalu_automation.core.device.base import DeviceAdapter
from daalu_automation.core.device.models import (
    ConfigDiff,
    Credentials,
    ExecutionResult,
    RenderedConfig,
)
from daalu_automation.core.device.registry import register_device_adapter
from daalu_automation.core.sot.models import (
    DeviceFacts,
    InterfaceConfig,
    NetworkFacts,
    StaticRoute,
    VlanDefinition,
)

logger = logging.getLogger(__name__)

RENDERER_VERSION = "eos.v1"


# ── CLI transport seam ───────────────────────────────────────────────


async def _open_cli(creds: Credentials) -> Any:
    """Open a Scrapli CLI session against an Arista EOS device.

    Tests monkeypatch this with a fake driver that records every call.
    """
    from scrapli import AsyncScrapli

    port = creds.port if creds.port not in (0, 0) else 22
    if creds.port == 22:
        port = 22
    driver = AsyncScrapli(
        platform="arista_eos",
        host=creds.host,
        port=port,
        auth_username=creds.user,
        auth_password=creds.password or "",
        auth_secondary=creds.enable_password or "",
        auth_strict_key=False,
        transport="asyncssh",
    )
    await driver.open()
    return driver


# ── Config-line builders ─────────────────────────────────────────────


def _interface_lines(iface: InterfaceConfig) -> list[str]:
    """EOS config-mode lines for one interface.

    Order matters: enter ``interface X`` first, then per-attribute
    lines, then an explicit ``no shutdown`` / ``shutdown`` based on
    ``enabled``. The trailing ``exit`` leaves config-mode at the
    interface scope.
    """
    lines: list[str] = [f"interface {iface.name}"]
    if iface.description is not None:
        lines.append(f"   description {iface.description}")
    if iface.mtu is not None:
        lines.append(f"   mtu {iface.mtu}")
    if iface.ipv4_address is not None:
        lines.append(f"   ip address {iface.ipv4_address}")
    if iface.vlan_access is not None:
        lines.append("   switchport mode access")
        lines.append(f"   switchport access vlan {iface.vlan_access}")
    lines.append("   no shutdown" if iface.enabled else "   shutdown")
    lines.append("exit")
    return lines


def _vlan_lines(vlan: VlanDefinition) -> list[str]:
    lines = [f"vlan {vlan.vlan_id}"]
    if vlan.name:
        lines.append(f"   name {vlan.name}")
    lines.append("exit")
    return lines


def _route_line(route: StaticRoute) -> str:
    if route.vrf:
        return f"ip route vrf {route.vrf} {route.prefix} {route.next_hop}"
    return f"ip route {route.prefix} {route.next_hop}"


def build_config_lines(facts: NetworkFacts) -> list[str]:
    """Flat list of config-mode lines that produces this intent.

    Visible at module scope so tests can pin the exact line sequence
    without going through the fake driver.
    """
    lines: list[str] = []
    if facts.hostname is not None:
        lines.append(f"hostname {facts.hostname}")
    for vlan in facts.vlans:
        lines.extend(_vlan_lines(vlan))
    for iface in facts.interfaces:
        lines.extend(_interface_lines(iface))
    for route in facts.static_routes:
        lines.append(_route_line(route))
    return lines


# ── Collect ──────────────────────────────────────────────────────────


def _parse_show_running(output: str, hint: NetworkFacts) -> NetworkFacts:
    """Pull NetworkFacts out of ``show running-config`` output.

    Restrictively scoped to the keys present in ``hint`` for
    interfaces — VLANs / routes are small enough to enumerate.
    """
    import re

    facts = NetworkFacts()

    m = re.search(r"^hostname\s+(\S+)\s*$", output, re.MULTILINE)
    if m:
        facts.hostname = m.group(1)

    # VLAN blocks: "vlan 100\n   name foo\n"
    for v_block in re.finditer(
        r"^vlan\s+(\d+)\s*$\n((?:\s+.*\n)*)",
        output,
        re.MULTILINE,
    ):
        vid = int(v_block.group(1))
        body = v_block.group(2)
        name_m = re.search(r"^\s+name\s+(\S+)", body, re.MULTILINE)
        facts.vlans.append(
            VlanDefinition(vlan_id=vid, name=name_m.group(1) if name_m else None)
        )

    # Interface blocks — only for names in hint.
    for hint_iface in hint.interfaces:
        name = re.escape(hint_iface.name)
        block = re.search(
            rf"^interface\s+{name}\s*$\n((?:\s+.*\n)*)",
            output,
            re.MULTILINE,
        )
        if not block:
            continue
        body = block.group(1)
        iface = InterfaceConfig(name=hint_iface.name)
        d = re.search(r"^\s+description\s+(.*?)\s*$", body, re.MULTILINE)
        if d:
            iface.description = d.group(1)
        mtu = re.search(r"^\s+mtu\s+(\d+)", body, re.MULTILINE)
        if mtu:
            iface.mtu = int(mtu.group(1))
        ip = re.search(r"^\s+ip address\s+(\S+)", body, re.MULTILINE)
        if ip:
            iface.ipv4_address = ip.group(1)
        va = re.search(r"^\s+switchport access vlan\s+(\d+)", body, re.MULTILINE)
        if va:
            iface.vlan_access = int(va.group(1))
        # EOS shows "no shutdown" as the absence of "shutdown"; treat
        # an explicit "shutdown" line as the only disabling signal.
        if re.search(r"^\s+shutdown\s*$", body, re.MULTILINE):
            iface.enabled = False
        facts.interfaces.append(iface)

    # Static routes — global + VRF forms.
    for r in re.finditer(
        r"^ip route(?:\s+vrf\s+(\S+))?\s+(\S+)\s+(\S+)\s*$",
        output,
        re.MULTILINE,
    ):
        facts.static_routes.append(
            StaticRoute(
                prefix=r.group(2),
                next_hop=r.group(3),
                vrf=r.group(1),
            )
        )
    return facts


# ── The adapter ──────────────────────────────────────────────────────


class EOSAdapter(DeviceAdapter):
    transport: ClassVar[str] = "eos"

    async def collect(
        self,
        creds: Credentials,
        intended_hint: DeviceFacts | None = None,
    ) -> NetworkFacts:
        hint = (
            intended_hint
            if isinstance(intended_hint, NetworkFacts)
            else NetworkFacts()
        )
        conn = await _open_cli(creds)
        try:
            resp = await conn.send_command("show running-config")
            output = _response_text(resp)
        finally:
            await _safe_close(conn)
        return _parse_show_running(output, hint)

    async def render(self, intended: DeviceFacts) -> RenderedConfig:
        if not isinstance(intended, NetworkFacts):
            raise TypeError(
                f"EOSAdapter.render expected NetworkFacts, got "
                f"{type(intended).__name__}"
            )
        return _net_common.render(intended, renderer_version=RENDERER_VERSION)

    async def diff(
        self, observed: DeviceFacts, intended: DeviceFacts
    ) -> ConfigDiff:
        if not isinstance(observed, NetworkFacts) or not isinstance(
            intended, NetworkFacts
        ):
            raise TypeError(
                "EOSAdapter.diff requires both observed and intended "
                "to be NetworkFacts"
            )
        return _net_common.diff(
            observed, intended, renderer_version=RENDERER_VERSION
        )

    async def execute(
        self, creds: Credentials, rendered: RenderedConfig
    ) -> ExecutionResult:
        started = datetime.now(tz=timezone.utc)
        intent = _net_common.facts_from_rendered(rendered)
        per_step: list[dict[str, Any]] = []
        conn: Any = None
        try:
            conn = await _open_cli(creds)
            per_step.append({"op": "cli.open", "ok": True})

            lines = build_config_lines(intent)
            if lines:
                await conn.send_configs(lines)
                per_step.append(
                    {"op": "send_configs", "lines": len(lines), "ok": True}
                )
            else:
                per_step.append(
                    {"op": "send_configs", "skipped": True, "reason": "no managed facts"}
                )

            # Persist running → startup. Equivalent to "write memory".
            await conn.send_command("copy running-config startup-config")
            per_step.append({"op": "save_startup", "ok": True})
        except Exception as e:  # noqa: BLE001 — adapter boundary
            await _safe_close(conn)
            return ExecutionResult(
                success=False,
                started_at=started.isoformat(),
                finished_at=datetime.now(tz=timezone.utc).isoformat(),
                per_step=per_step,
                rollback_performed=False,
                error=f"{type(e).__name__}: {e}",
            )

        await _safe_close(conn)
        return ExecutionResult(
            success=True,
            started_at=started.isoformat(),
            finished_at=datetime.now(tz=timezone.utc).isoformat(),
            per_step=per_step,
            rollback_performed=False,
            error="",
        )


def _response_text(resp: Any) -> str:
    if isinstance(resp, str):
        return resp
    for attr in ("result", "channel_input"):
        value = getattr(resp, attr, None)
        if isinstance(value, str) and value:
            return value
    return str(resp)


async def _safe_close(conn: Any) -> None:
    if conn is None:
        return
    try:
        close = getattr(conn, "close", None)
        if close is None:
            return
        result = close()
        if hasattr(result, "__await__"):
            await result
    except Exception:  # noqa: BLE001
        logger.warning("eos.cli.close_failed", exc_info=True)


register_device_adapter(EOSAdapter)
