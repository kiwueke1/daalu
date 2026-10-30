"""Juniper Junos device adapter (NETCONF over SSH).

Talks to a Junos device via NETCONF on port 830 using
:mod:`scrapli_netconf`. The Junos NETCONF surface is mature and
exposes commit-confirmed natively, which is what we lean on for safe
rollout — see ``execute`` below.

In-scope facts (mirroring :class:`NetworkFacts`):

* hostname (``system/host-name``)
* interfaces — name / description / enabled / mtu / ipv4 / vlan_access
* vlans — vlan_id / name
* static routes — prefix / next_hop / optional vrf

Out of scope for v1: BGP, OSPF, ACLs, route-maps, prefix-lists,
multicast, MPLS. Add a follow-up issue rather than extending here.

Execute pattern (commit-confirmed → confirm):

1. ``<edit-config>`` against ``candidate`` with the rendered
   ``<configuration>`` payload.
2. ``<commit><confirmed/><confirm-timeout>N</confirm-timeout></commit>``
   — applies the change with an auto-rollback timer.
3. Brief settle window (the adapter waits ``_SETTLE_SECONDS`` so the
   device has a moment to re-converge before the second commit).
4. Plain ``<commit/>`` — confirms the change. If our second call
   never reaches the device (our connection died, the device became
   unreachable, the executor crashed mid-step), Junos auto-reverts at
   step 2's timer.

If any step raises, ``ExecutionResult.success=False`` carries the
error and per-step trace, and the operator sees the failure in the
proposal's execution evidence. We don't issue an explicit
``<discard-changes/>`` — the commit-confirmed timer is the safety
net, and an explicit rollback adds a third remote call that can fail
for its own reasons.
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
    VlanDefinition,
)

logger = logging.getLogger(__name__)

RENDERER_VERSION = "junos.v1"
# Seconds to wait between commit-confirmed and the follow-up confirm.
# Kept short — its purpose is to let the device finish applying before
# we re-issue, not to verify anything. Tests monkeypatch this to 0 so
# the suite doesn't sleep.
_SETTLE_SECONDS = 1.0

NETCONF_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"


# ── NETCONF transport seam ───────────────────────────────────────────


async def _open_netconf(creds: Credentials) -> Any:
    """Open a scrapli-netconf session.

    Factored out as a module-level coroutine so tests have a single
    monkeypatch target — same pattern :mod:`redfish._build_http_client`
    uses for the BMC client.
    """
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
    return f"<system><host-name>{hostname}</host-name></system>"


def _xml_interface(iface: InterfaceConfig) -> str:
    parts: list[str] = [f"<name>{iface.name}</name>"]
    if iface.description is not None:
        parts.append(f"<description>{iface.description}</description>")
    if iface.mtu is not None:
        parts.append(f"<mtu>{iface.mtu}</mtu>")
    # Junos "disable" leaf is presence-only — present means disabled,
    # absent means enabled. Emit only when explicitly disabling so a
    # re-enable also takes (since we never set "enabled=true" leaf).
    if iface.enabled is False:
        parts.append("<disable/>")
    # IPv4 / VLAN-access live under unit 0 — v1 only manages the first
    # logical unit, which covers ge-/xe-/et- L3 and access-port L2.
    unit_parts: list[str] = []
    if iface.ipv4_address is not None:
        unit_parts.append(
            "<family><inet><address>"
            f"<name>{iface.ipv4_address}</name>"
            "</address></inet></family>"
        )
    if iface.vlan_access is not None:
        unit_parts.append(
            f"<family><ethernet-switching><vlan>"
            f"<members>{iface.vlan_access}</members>"
            f"</vlan></ethernet-switching></family>"
        )
    if unit_parts:
        parts.append("<unit><name>0</name>" + "".join(unit_parts) + "</unit>")
    return "<interface>" + "".join(parts) + "</interface>"


def _xml_vlan(vlan: VlanDefinition) -> str:
    body = f"<vlan-id>{vlan.vlan_id}</vlan-id>"
    name = vlan.name or f"vlan{vlan.vlan_id}"
    return f"<vlan><name>{name}</name>{body}</vlan>"


def _xml_route(route: StaticRoute) -> str:
    inner = (
        f"<name>{route.prefix}</name>"
        f"<next-hop>{route.next_hop}</next-hop>"
    )
    if route.vrf:
        # Junos non-default VRFs live under routing-instances/<name>/
        # routing-options/static. We emit that nested form so a route
        # tagged with vrf="customer-a" ends up in the right RIB.
        return (
            f"<routing-instances><instance>"
            f"<name>{route.vrf}</name>"
            f"<routing-options><static><route>{inner}</route></static></routing-options>"
            f"</instance></routing-instances>"
        )
    return f"<routing-options><static><route>{inner}</route></static></routing-options>"


def build_edit_config(facts: NetworkFacts) -> str:
    """Build a <config> payload for ``edit-config`` from NetworkFacts.

    Returns the bare ``<config>...</config>`` element — the scrapli-
    netconf ``edit_config`` wraps it in the rpc envelope. Visible at
    module scope so tests can pin its content without going through a
    fake driver.
    """
    body_parts: list[str] = []
    if facts.hostname is not None:
        body_parts.append(_xml_hostname(facts.hostname))
    if facts.interfaces:
        body_parts.append(
            "<interfaces>"
            + "".join(_xml_interface(i) for i in facts.interfaces)
            + "</interfaces>"
        )
    if facts.vlans:
        body_parts.append(
            "<vlans>"
            + "".join(_xml_vlan(v) for v in facts.vlans)
            + "</vlans>"
        )
    for route in facts.static_routes:
        body_parts.append(_xml_route(route))
    inner = "".join(body_parts)
    return f"<config><configuration>{inner}</configuration></config>"


def build_commit_confirmed_rpc(timeout_s: int) -> str:
    """Junos commit-confirmed RPC body."""
    return (
        f'<commit-configuration><confirmed/>'
        f"<confirm-timeout>{timeout_s}</confirm-timeout>"
        f"</commit-configuration>"
    )


def build_commit_rpc() -> str:
    return "<commit-configuration/>"


# ── Collect (get-config with subtree filter) ─────────────────────────


def _collect_filter(hint: NetworkFacts) -> str:
    """Build a NETCONF subtree filter scoped to the keys in intent.

    For hostname / VLANs / static routes we always fetch (small
    payload). For interfaces we scope to the names in ``hint`` so we
    don't enumerate the full chassis.
    """
    parts: list[str] = ["<system><host-name/></system>"]
    if hint.interfaces:
        ifs = "".join(
            f"<interface><name>{i.name}</name></interface>"
            for i in hint.interfaces
        )
        parts.append(f"<interfaces>{ifs}</interfaces>")
    parts.append("<vlans/>")
    parts.append("<routing-options><static/></routing-options>")
    return "<configuration>" + "".join(parts) + "</configuration>"


def _parse_get_config(xml_text: str, hint: NetworkFacts) -> NetworkFacts:
    """Pull the few fields we manage out of a Junos get-config reply.

    Intentionally narrow: we don't lean on lxml/xpath because the
    surface we care about is small and a substring/regex parse keeps
    tests trivial. The trade-off is real — if the device returns
    deeply nested vendor-specific elements we'd miss them — but for
    v1's hostname / iface name / vlan id / route prefix this is
    sufficient.
    """
    import re

    facts = NetworkFacts()
    m = re.search(r"<host-name>([^<]+)</host-name>", xml_text)
    if m:
        facts.hostname = m.group(1).strip()
    # Interfaces — only collect names listed in hint.
    for hint_iface in hint.interfaces:
        name = re.escape(hint_iface.name)
        block = re.search(
            rf"<interface>\s*<name>{name}</name>(.*?)</interface>",
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
        if "<disable/>" in body or "<disable></disable>" in body:
            iface.enabled = False
        mtu = re.search(r"<mtu>(\d+)</mtu>", body)
        if mtu:
            iface.mtu = int(mtu.group(1))
        ipm = re.search(r"<family>\s*<inet>\s*<address>\s*<name>([^<]+)</name>", body)
        if ipm:
            iface.ipv4_address = ipm.group(1)
        vlm = re.search(
            r"<ethernet-switching>.*?<vlan>\s*<members>([^<]+)</members>",
            body,
            re.DOTALL,
        )
        if vlm:
            try:
                iface.vlan_access = int(vlm.group(1).strip())
            except ValueError:
                pass
        facts.interfaces.append(iface)
    # VLANs — full list (small).
    for vlan_block in re.finditer(
        r"<vlan>\s*<name>([^<]+)</name>\s*<vlan-id>(\d+)</vlan-id>",
        xml_text,
    ):
        facts.vlans.append(
            VlanDefinition(
                vlan_id=int(vlan_block.group(2)),
                name=vlan_block.group(1) or None,
            )
        )
    # Static routes — global VRF only (v1 simplification; named-VRF
    # routes appear under routing-instances and we don't enumerate
    # those yet).
    for route_block in re.finditer(
        r"<static>\s*<route>\s*<name>([^<]+)</name>\s*<next-hop>([^<]+)</next-hop>",
        xml_text,
    ):
        facts.static_routes.append(
            StaticRoute(
                prefix=route_block.group(1),
                next_hop=route_block.group(2),
            )
        )
    return facts


# ── The adapter ──────────────────────────────────────────────────────


class JunosAdapter(DeviceAdapter):
    transport: ClassVar[str] = "junos"

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
                f"JunosAdapter.render expected NetworkFacts, got "
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
                "JunosAdapter.diff requires both observed and intended "
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

            # Brief settle so the device has a moment before the
            # follow-up confirm. If this sleep is interrupted by the
            # executor task being cancelled, the commit-confirmed timer
            # on the device handles the auto-rollback.
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
    """Extract the response XML from a scrapli-netconf response object.

    Tests pass either a string (fake driver returning raw XML) or an
    object exposing ``.result`` / ``.xml_result`` / ``.channel_input``;
    real scrapli-netconf returns NetconfResponse with ``.result``.
    """
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
    except Exception:  # noqa: BLE001 — close is best-effort
        logger.warning("junos.netconf.close_failed", exc_info=True)


register_device_adapter(JunosAdapter)
