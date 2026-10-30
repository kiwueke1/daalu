"""Shared render / diff helpers for the network-OS adapters.

Junos, IOS-XR, and Arista EOS all carry the same :class:`NetworkFacts`
shape, so the rendered config layout and the diff body are identical
between them — only ``renderer_version`` (and of course the transport
implementation of ``collect`` / ``execute``) differs.

Keeping these helpers in one place means a renderer bump touches one
file rather than three, and the stale-detection contract is shared
across the vendors.

Rendered-config layout (used by every NetworkFacts adapter):

* ``/_net/hostname.json`` — ``{"hostname": "..."}``
* ``/_net/interfaces/<name>.json`` — one file per managed interface
* ``/_net/vlans.json`` — single sorted list
* ``/_net/static_routes.json`` — single list sorted by ``(vrf, prefix)``

Per-interface files (rather than a single bundle) means the executor's
byte-for-byte stale check only re-fires on the interface that actually
changed, so an unrelated parallel proposal doesn't go stale just
because something else in interfaces[] was touched.
"""

from __future__ import annotations

import difflib
import json
import logging
from typing import Any

from daalu_automation.core.device.models import ConfigDiff, RenderedConfig
from daalu_automation.core.sot.models import (
    InterfaceConfig,
    NetworkFacts,
    StaticRoute,
    VlanDefinition,
)

logger = logging.getLogger(__name__)

PATH_HOSTNAME = "/_net/hostname.json"
PATH_INTERFACES_DIR = "/_net/interfaces"
PATH_VLANS = "/_net/vlans.json"
PATH_STATIC_ROUTES = "/_net/static_routes.json"


def _interface_path(name: str) -> str:
    # Interface names contain "/", which is fine for our virtual paths
    # — the diff engine never touches the real filesystem with them.
    # Replacing with "_" would lose information (ge-0/0/0 vs ge-0_0_0
    # collide with weirdly-named neighbors).
    return f"{PATH_INTERFACES_DIR}/{name}.json"


def _dump(blob: Any) -> str:
    return json.dumps(blob, sort_keys=True, indent=2) + "\n"


def render(facts: NetworkFacts, *, renderer_version: str) -> RenderedConfig:
    """Render NetworkFacts to the deterministic file mapping.

    Only emits keys for facts the operator actually manages — an empty
    intent renders to an empty files map (and the diff treats that as
    "no managed concerns", same as LinuxSSH / Redfish).
    """
    files: dict[str, str] = {}
    if facts.hostname is not None:
        files[PATH_HOSTNAME] = _dump({"hostname": facts.hostname})
    for iface in facts.interfaces:
        # Sorted by attribute name via json sort_keys, so a re-render
        # with the same intent produces the same bytes.
        files[_interface_path(iface.name)] = _dump(iface.model_dump())
    if facts.vlans:
        vlans_sorted = sorted(
            (v.model_dump() for v in facts.vlans), key=lambda v: v["vlan_id"]
        )
        files[PATH_VLANS] = _dump({"vlans": vlans_sorted})
    if facts.static_routes:
        routes_sorted = sorted(
            (r.model_dump() for r in facts.static_routes),
            key=lambda r: (r.get("vrf") or "", r["prefix"]),
        )
        files[PATH_STATIC_ROUTES] = _dump({"static_routes": routes_sorted})

    parts: list[str] = []
    if facts.hostname:
        parts.append(f"hostname={facts.hostname}")
    if facts.interfaces:
        parts.append(f"interfaces[{len(facts.interfaces)}]")
    if facts.vlans:
        parts.append(f"vlans[{len(facts.vlans)}]")
    if facts.static_routes:
        parts.append(f"routes[{len(facts.static_routes)}]")
    summary = " · ".join(parts) or "(no managed facts)"

    return RenderedConfig(
        renderer_version=renderer_version, files=files, summary=summary
    )


def _routes_key(r: StaticRoute) -> tuple[str, str]:
    return (r.vrf or "", r.prefix)


def diff(
    observed: NetworkFacts, intended: NetworkFacts, *, renderer_version: str
) -> ConfigDiff:
    """Per-fact diff between observed and intended NetworkFacts.

    Mirrors the Redfish pattern: walk per-fact, collect a list of
    changed keys, then if anything changed render both sides and emit
    a unified diff per rendered file.
    """
    facts_changed: list[str] = []

    if intended.hostname is not None and intended.hostname != observed.hostname:
        facts_changed.append("hostname")

    obs_ifaces = {i.name: i for i in observed.interfaces}
    int_ifaces = {i.name: i for i in intended.interfaces}
    for name, iface in int_ifaces.items():
        if obs_ifaces.get(name) != iface:
            facts_changed.append(f"interface.{name}")

    obs_vlans = {v.vlan_id: v for v in observed.vlans}
    int_vlans = {v.vlan_id: v for v in intended.vlans}
    for vid, vlan in int_vlans.items():
        if obs_vlans.get(vid) != vlan:
            facts_changed.append(f"vlan.{vid}")

    obs_routes = {_routes_key(r): r for r in observed.static_routes}
    int_routes = {_routes_key(r): r for r in intended.static_routes}
    for key, route in int_routes.items():
        if obs_routes.get(key) != route:
            vrf_part = f"{key[0]}:" if key[0] else ""
            facts_changed.append(f"route.{vrf_part}{key[1]}")

    if not facts_changed:
        return ConfigDiff(facts_changed=[], unified_diff="", has_changes=False)

    obs_files = render(observed, renderer_version=renderer_version).files
    int_files = render(intended, renderer_version=renderer_version).files
    lines: list[str] = []
    for path in sorted(set(obs_files) | set(int_files)):
        a = obs_files.get(path, "")
        b = int_files.get(path, "")
        if a == b:
            continue
        lines.extend(
            difflib.unified_diff(
                a.splitlines(keepends=True),
                b.splitlines(keepends=True),
                fromfile=f"a{path}",
                tofile=f"b{path}",
                n=3,
            )
        )

    return ConfigDiff(
        facts_changed=facts_changed,
        unified_diff="".join(lines),
        has_changes=True,
    )


def facts_from_rendered(rendered: RenderedConfig) -> NetworkFacts:
    """Re-parse a rendered files map back into NetworkFacts.

    The executor compares ``rendered`` byte-for-byte against the
    approved snapshot, so this round-trip is purely for the adapter's
    own use during ``execute``: we want to operate on the snapshot the
    operator approved, not on whatever current intent happens to be.
    """
    facts = NetworkFacts()
    if PATH_HOSTNAME in rendered.files:
        try:
            data = json.loads(rendered.files[PATH_HOSTNAME])
            facts.hostname = data.get("hostname")
        except json.JSONDecodeError:
            logger.warning("net.rendered.hostname.bad_json")

    for path, body in rendered.files.items():
        if not path.startswith(PATH_INTERFACES_DIR + "/"):
            continue
        try:
            data = json.loads(body)
            facts.interfaces.append(InterfaceConfig(**data))
        except Exception:
            logger.warning("net.rendered.interface.bad_json", extra={"path": path})

    if PATH_VLANS in rendered.files:
        try:
            data = json.loads(rendered.files[PATH_VLANS])
            for v in data.get("vlans") or []:
                facts.vlans.append(VlanDefinition(**v))
        except Exception:
            logger.warning("net.rendered.vlans.bad_json")

    if PATH_STATIC_ROUTES in rendered.files:
        try:
            data = json.loads(rendered.files[PATH_STATIC_ROUTES])
            for r in data.get("static_routes") or []:
                facts.static_routes.append(StaticRoute(**r))
        except Exception:
            logger.warning("net.rendered.routes.bad_json")

    # Re-sort interfaces by name so the round-trip is deterministic.
    # (render() sorts the *files map* but the list order out of the
    # rendered map depends on dict ordering at parse time.)
    facts.interfaces.sort(key=lambda i: i.name)
    return facts
