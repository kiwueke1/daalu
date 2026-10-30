"""Cisco IOS-XR device adapter (NETCONF over SSH).

Same shape as :mod:`daalu_automation.core.device.junos`: NETCONF on
port 830 via :mod:`scrapli_netconf`, edit-config against ``candidate``,
RFC-6241 commit-confirmed → confirm pattern.

The XML payloads differ from Junos because IOS-XR uses Cisco-IOS-XR
YANG modules (``Cisco-IOS-XR-shellutil-cfg``,
``Cisco-IOS-XR-ifmgr-cfg``, ``Cisco-IOS-XR-ip-static-cfg``), but
control flow is identical:

1. ``<edit-config target=candidate>``
2. ``<commit><confirmed/><confirm-timeout>N</confirm-timeout></commit>``
   — RFC 6241 §8.3 / §8.4, supported by IOS-XR's native NETCONF.
3. Brief settle.
4. Plain ``<commit/>`` — confirms the change.

VLANs and L2 are intentionally out of scope: IOS-XR's L2 surface is
heterogeneous (l2vpn bridge-domains vs sub-interface encapsulation)
and warrants its own follow-up. ``NetworkFacts.vlans`` on an IOS-XR
device is silently dropped at execute time (with a per-step note)
rather than failing the proposal.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, ClassVar

from daalu_automation.config import get_settings
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
)

logger = logging.getLogger(__name__)

RENDERER_VERSION = "iosxr.v1"
_SETTLE_SECONDS = 1.0

NS_SHELL = "http://cisco.com/ns/yang/Cisco-IOS-XR-shellutil-cfg"
NS_IFMGR = "http://cisco.com/ns/yang/Cisco-IOS-XR-ifmgr-cfg"
NS_IPV4_IO = "http://cisco.com/ns/yang/Cisco-IOS-XR-ipv4-io-cfg"
NS_STATIC = "http://cisco.com/ns/yang/Cisco-IOS-XR-ip-static-cfg"


# ── NETCONF transport seam ───────────────────────────────────────────


async def _open_netconf(creds: Credentials) -> Any:
    from scrapli_netconf import AsyncNetconfDriver

    port = creds.port if creds.port not in (0, 22) else 830
    driver = AsyncNetconfDriver(
        host=creds.host,
        port=port,
        auth_username=creds.user,
        auth_password=creds.password or "",
        auth_strict_key=False,
        transport="asyncssh",
    )
    await driver.open()
    return driver


# ── Edit-config XML builders ─────────────────────────────────────────


def _xml_hostname(hostname: str) -> str:
    return (
        f'<host-names xmlns="{NS_SHELL}">'
        f"<host-name>{hostname}</host-name>"
        f"</host-names>"
    )


def _split_cidr(addr: str) -> tuple[str, str]:
    """Split a CIDR into (address, dotted-decimal netmask).

    IOS-XR's ipv4 network config wants address + netmask, not CIDR.
    For non-/N inputs we default to /32 (host route).
    """
    if "/" not in addr:
        return addr, "255.255.255.255"
    ip, prefix = addr.split("/", 1)
    try:
        bits = int(prefix)
    except ValueError:
        return ip, "255.255.255.255"
    mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
    octets = [(mask >> (24 - 8 * i)) & 0xFF for i in range(4)]
    return ip, ".".join(str(o) for o in octets)


def _xml_interface(iface: InterfaceConfig) -> str:
    parts: list[str] = [
        f"<interface-name>{iface.name}</interface-name>",
        # "active" leaf: "act" means the interface is administratively
        # active (corresponds to "no shutdown"). "preconfigure" is the
        # alternative; we always use "act".
        "<active>act</active>",
    ]
    if iface.description is not None:
        parts.append(f"<description>{iface.description}</description>")
    if iface.enabled is False:
        parts.append("<shutdown/>")
    if iface.mtu is not None:
        parts.append(f"<mtu>{iface.mtu}</mtu>")
    if iface.ipv4_address is not None:
        ip, mask = _split_cidr(iface.ipv4_address)
        parts.append(
            f'<ipv4-network xmlns="{NS_IPV4_IO}">'
            f"<addresses><primary>"
            f"<address>{ip}</address>"
            f"<netmask>{mask}</netmask>"
            f"</primary></addresses>"
            f"</ipv4-network>"
        )
    # ``vlan_access`` on IOS-XR is sub-interface encapsulation; v1
    # leaves it out (see module docstring) but we keep the leaf in the
    # rendered file for round-trippability — the executor still pushes
    # the rest of the interface fields.
    return (
        '<interface-configuration>'
        + "".join(parts)
        + '</interface-configuration>'
    )


def _xml_static_route(route: StaticRoute) -> str:
    if "/" not in route.prefix:
        ip, prefix_len = route.prefix, "32"
    else:
        ip, prefix_len = route.prefix.split("/", 1)
    body = (
        f"<prefix>{ip}</prefix>"
        f"<prefix-length>{prefix_len}</prefix-length>"
        f"<vrf-route><vrf-next-hop-table>"
        f"<vrf-next-hop-next-hop-address>"
        f"<next-hop-address>{route.next_hop}</next-hop-address>"
        f"</vrf-next-hop-next-hop-address>"
        f"</vrf-next-hop-table></vrf-route>"
    )
    if route.vrf:
        return (
            f'<router-static xmlns="{NS_STATIC}">'
            f"<vrfs><vrf>"
            f"<vrf-name>{route.vrf}</vrf-name>"
            f"<address-family><vrfipv4><vrf-unicast>"
            f"<vrf-prefixes><vrf-prefix>{body}</vrf-prefix></vrf-prefixes>"
            f"</vrf-unicast></vrfipv4></address-family>"
            f"</vrf></vrfs>"
            f"</router-static>"
        )
    return (
        f'<router-static xmlns="{NS_STATIC}">'
        f"<default-vrf><address-family><vrfipv4><vrf-unicast>"
        f"<vrf-prefixes><vrf-prefix>{body}</vrf-prefix></vrf-prefixes>"
        f"</vrf-unicast></vrfipv4></address-family></default-vrf>"
        f"</router-static>"
    )


def build_edit_config(facts: NetworkFacts) -> str:
    body_parts: list[str] = []
    if facts.hostname is not None:
        body_parts.append(_xml_hostname(facts.hostname))
    if facts.interfaces:
        body_parts.append(
            f'<interface-configurations xmlns="{NS_IFMGR}">'
            + "".join(_xml_interface(i) for i in facts.interfaces)
            + "</interface-configurations>"
        )
    for route in facts.static_routes:
        body_parts.append(_xml_static_route(route))
    inner = "".join(body_parts)
    return f"<config>{inner}</config>"


def build_commit_confirmed_rpc(timeout_s: int) -> str:
    """RFC-6241 §8.4 commit-confirmed RPC body.

    Standard NETCONF — IOS-XR speaks this natively. Junos uses its
    own ``<commit-configuration>`` form; that's why the RPC builder
    lives per-adapter rather than shared.
    """
    return (
        f"<commit><confirmed/><confirm-timeout>{timeout_s}</confirm-timeout></commit>"
    )


def build_commit_rpc() -> str:
    return "<commit/>"


# ── Collect ──────────────────────────────────────────────────────────


def _collect_filter(hint: NetworkFacts) -> str:
    parts: list[str] = [f'<host-names xmlns="{NS_SHELL}"/>']
    if hint.interfaces:
        ifs = "".join(
            f"<interface-configuration><interface-name>{i.name}</interface-name>"
            f"</interface-configuration>"
            for i in hint.interfaces
        )
        parts.append(
            f'<interface-configurations xmlns="{NS_IFMGR}">{ifs}'
            f"</interface-configurations>"
        )
    parts.append(f'<router-static xmlns="{NS_STATIC}"/>')
    return "".join(parts)


def _parse_get_config(xml_text: str, hint: NetworkFacts) -> NetworkFacts:
    import re

    facts = NetworkFacts()
    m = re.search(r"<host-name>([^<]+)</host-name>", xml_text)
    if m:
        facts.hostname = m.group(1).strip()
    for hint_iface in hint.interfaces:
        name = re.escape(hint_iface.name)
        block = re.search(
            rf"<interface-configuration>.*?<interface-name>{name}</interface-name>(.*?)</interface-configuration>",
            xml_text,
            re.DOTALL,
        )
        if not block:
            continue
        body = block.group(1)
        iface = InterfaceConfig(name=hint_iface.name)
        d = re.search(r"<description>([^<]*)</description>", body)
        if d:
            iface.description = d.group(1)
        if "<shutdown/>" in body or "<shutdown></shutdown>" in body:
            iface.enabled = False
        mtu = re.search(r"<mtu>(\d+)</mtu>", body)
        if mtu:
            iface.mtu = int(mtu.group(1))
        ipm = re.search(r"<address>([^<]+)</address>", body)
        nm = re.search(r"<netmask>([^<]+)</netmask>", body)
        if ipm and nm:
            iface.ipv4_address = f"{ipm.group(1)}/{_mask_to_prefix(nm.group(1))}"
        elif ipm:
            iface.ipv4_address = ipm.group(1)
        facts.interfaces.append(iface)
    for route_block in re.finditer(
        r"<prefix>([^<]+)</prefix>\s*<prefix-length>(\d+)</prefix-length>.*?<next-hop-address>([^<]+)</next-hop-address>",
        xml_text,
        re.DOTALL,
    ):
        facts.static_routes.append(
            StaticRoute(
                prefix=f"{route_block.group(1)}/{route_block.group(2)}",
                next_hop=route_block.group(3),
            )
        )
    return facts


def _mask_to_prefix(mask: str) -> int:
    """Convert a dotted-decimal netmask to a prefix length."""
    try:
        octets = [int(o) for o in mask.split(".")]
        bits = 0
        for o in octets:
            bits += bin(o).count("1")
        return bits
    except (ValueError, AttributeError):
        return 32


# ── The adapter ──────────────────────────────────────────────────────


class IOSXRAdapter(DeviceAdapter):
    transport: ClassVar[str] = "iosxr"

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
        conn = await _open_netconf(creds)
        try:
            filt = _collect_filter(hint)
            resp = await conn.get_config(source="running", filter_=filt)
            xml_text = _response_xml(resp)
        finally:
            await _safe_close(conn)
        return _parse_get_config(xml_text, hint)

    async def render(self, intended: DeviceFacts) -> RenderedConfig:
        if not isinstance(intended, NetworkFacts):
            raise TypeError(
                f"IOSXRAdapter.render expected NetworkFacts, got "
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
                "IOSXRAdapter.diff requires both observed and intended "
                "to be NetworkFacts"
            )
        return _net_common.diff(
            observed, intended, renderer_version=RENDERER_VERSION
        )

    async def execute(
        self, creds: Credentials, rendered: RenderedConfig
    ) -> ExecutionResult:
        started = datetime.now(tz=timezone.utc)
        settings = get_settings()
        timeout_s = settings.commit_confirmed_timeout_s
        intent = _net_common.facts_from_rendered(rendered)
        per_step: list[dict[str, Any]] = []
        conn: Any = None
        if intent.vlans:
            per_step.append(
                {
                    "op": "vlans.skipped",
                    "note": (
                        "IOS-XR L2/VLAN config is out of scope for "
                        "NetworkFacts v1 — vlans[] in intent is recorded "
                        "but not pushed by this adapter"
                    ),
                }
            )
        try:
            conn = await _open_netconf(creds)
            per_step.append({"op": "netconf.open", "ok": True})

            xml_payload = build_edit_config(intent)
            await conn.edit_config(config=xml_payload, target="candidate")
            per_step.append({"op": "edit_config", "target": "candidate", "ok": True})

            await conn.rpc(filter_=build_commit_confirmed_rpc(timeout_s))
            per_step.append(
                {"op": "commit_confirmed", "timeout_s": timeout_s, "ok": True}
            )

            await asyncio.sleep(_SETTLE_SECONDS)

            await conn.rpc(filter_=build_commit_rpc())
            per_step.append({"op": "commit", "ok": True})
        except Exception as e:  # noqa: BLE001 — adapter boundary
            per_step.append(
                {
                    "op": "rollback_via_commit_confirmed_timer",
                    "note": (
                        "device will auto-rollback when the "
                        f"{timeout_s}s commit-confirmed window expires"
                    ),
                }
            )
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


def _response_xml(resp: Any) -> str:
    if isinstance(resp, str):
        return resp
    for attr in ("result", "xml_result", "channel_input"):
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
        logger.warning("iosxr.netconf.close_failed", exc_info=True)


register_device_adapter(IOSXRAdapter)
