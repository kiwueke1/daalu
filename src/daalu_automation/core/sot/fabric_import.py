"""Fabric-plan parsing for the /operations → Bulk import flow.

Where :mod:`bulk_import` turns a file into *devices*, this turns a file into
the rest of the model — the three artifacts that take an inventory and make it
a source of truth:

* **cabling** — ``interfaces:`` and ``cables:``. What ports exist and what is
  supposed to be plugged into what.
* **addressing** — ``prefixes:``, ``loopbacks:`` and ``point_to_point:``. The
  numbers a config contains.
* **intent** — ``roles:`` and ``config_contexts:``. What "correct" means for a
  box, as structured data rather than config.

Each file declares which one it is with a top-level ``kind:``. That line is
what picks the importer, so a file dropped on the wrong tab is rejected with a
message naming both — rather than silently importing nothing.

Order matters *between* files as well as within them: a cable can only
terminate on an interface that already exists, and an address can only sit on
an interface that already exists. So cabling must be applied before addressing.
:data:`ARTIFACT_ORDER` records that, and the router enforces it.

The parser is deliberately format-level only. It checks that a row is
*shaped* right — endpoints look like ``device:port``, addresses parse as CIDR —
and leaves "does this device exist in Nautobot" to the applier, which is where
the tenant-scoped HTTP client lives. Same split as :mod:`bulk_import`.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

import yaml

#: The three artifacts, in the order they must be applied.
ARTIFACT_ORDER = ("cabling", "addressing", "intent")

#: Top-level sections each artifact owns. Used to infer ``kind`` when a file
#: does not declare one (hand-written files, or the pre-split plan).
ARTIFACT_SECTIONS: dict[str, tuple[str, ...]] = {
    "cabling": ("interfaces", "cables"),
    "addressing": ("prefixes", "loopbacks", "point_to_point"),
    "intent": ("roles", "config_contexts"),
}

#: Sections that carry no rows of their own.
_META_KEYS = {"kind", "meta"}

DEFAULT_NAMESPACE = "Global"
DEFAULT_INTERFACE_TYPE = "10gbase-x-sfpp"

#: Nautobot's cable status for a link the model asserts should exist.
CABLE_STATUS = "Connected"


@dataclass
class FabricRow:
    """One row of a fabric plan, pre-resolution against Nautobot.

    ``row_index`` is 1-based within the artifact, not within the file — the
    preview table numbers what the user is about to create, and interfaces and
    cables are two different runs of objects in one file.

    ``op`` is what the applier will do with it; ``target`` and ``detail`` are
    display-only, and are what the preview table's two identifying columns show.
    """

    row_index: int
    op: str
    target: str
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    parse_error: str | None = None


class FabricParseError(ValueError):
    """The file could not be read as a fabric plan at all.

    Distinct from a row-level ``parse_error``: this means there is nothing to
    preview, so the router turns it into a 400 rather than a table of failures.
    """


def _split_endpoint(value: str) -> tuple[str, str]:
    """``"leaf1:swp1"`` → ``("leaf1", "swp1")``. Raises on anything else."""
    text = str(value).strip()
    if text.count(":") != 1:
        raise ValueError(f"endpoint '{text}' is not in device:interface form")
    device, interface = (part.strip() for part in text.split(":"))
    if not device or not interface:
        raise ValueError(f"endpoint '{text}' is missing a device or an interface")
    return device, interface


def detect_kind(doc: dict[str, Any]) -> str:
    """Return the artifact this document is, by declaration or by shape.

    A ``kind:`` line wins outright. Without one, infer from which sections are
    present — but refuse a file that spans two artifacts, because applying half
    of it in the wrong order is exactly the failure this split exists to stop.
    """
    declared = doc.get("kind")
    if declared is not None:
        kind = str(declared).strip().lower()
        if kind not in ARTIFACT_ORDER:
            raise FabricParseError(
                f"unknown kind '{declared}' — expected one of: {', '.join(ARTIFACT_ORDER)}"
            )
        return kind

    present = {k for k in doc if k not in _META_KEYS}
    matched = [a for a, sections in ARTIFACT_SECTIONS.items() if present & set(sections)]
    if not matched:
        raise FabricParseError(
            "no fabric sections found — expected one of: "
            + ", ".join(sorted(s for v in ARTIFACT_SECTIONS.values() for s in v))
        )
    if len(matched) > 1:
        raise FabricParseError(
            f"file mixes {' and '.join(sorted(matched))} sections — upload them as "
            "separate files so they can be applied in order"
        )
    return matched[0]


def _parse_cabling(doc: dict[str, Any]) -> list[FabricRow]:
    """``interfaces:`` first, then ``cables:`` — the order they must be created."""
    rows: list[FabricRow] = []
    index = 0

    interfaces = doc.get("interfaces") or {}
    if interfaces and not isinstance(interfaces, dict):
        raise FabricParseError("'interfaces:' must be a mapping of device -> port list")
    for device, ports in interfaces.items():
        for entry in ports or []:
            index += 1
            if isinstance(entry, str):
                entry = {"name": entry}
            if not isinstance(entry, dict):
                rows.append(FabricRow(index, "interface", str(device), parse_error="not a mapping"))
                continue
            name = str(entry.get("name", "")).strip()
            itype = str(entry.get("type") or DEFAULT_INTERFACE_TYPE).strip()
            rows.append(
                FabricRow(
                    row_index=index,
                    op="interface",
                    target=f"{device}:{name}" if name else str(device),
                    detail=itype,
                    payload={"device": str(device).strip(), "name": name, "type": itype},
                    parse_error=None if name else "missing interface name",
                )
            )

    cables = doc.get("cables") or []
    if cables and not isinstance(cables, list):
        raise FabricParseError("'cables:' must be a list")
    for entry in cables:
        index += 1
        # Two accepted spellings: a two-item list (what the generator emits,
        # because it reads as a link rather than as a direction), or a mapping
        # with `a`/`b` keys for anyone hand-writing one.
        if isinstance(entry, dict):
            pair = [entry.get("a"), entry.get("b")]
        elif isinstance(entry, (list, tuple)):
            pair = list(entry)
        else:
            rows.append(FabricRow(index, "cable", str(entry), parse_error="not a link"))
            continue
        if len(pair) != 2 or not all(pair):
            rows.append(
                FabricRow(index, "cable", str(entry), parse_error="a cable needs exactly two ends")
            )
            continue
        # Both ends are non-empty by the guard above; bind them so the types
        # say so too, rather than leaving `Any | None` for the reader.
        raw_a, raw_b = str(pair[0]), str(pair[1])
        try:
            dev_a, port_a = _split_endpoint(raw_a)
            dev_b, port_b = _split_endpoint(raw_b)
        except ValueError as exc:
            rows.append(FabricRow(index, "cable", f"{raw_a} ↔ {raw_b}", parse_error=str(exc)))
            continue
        rows.append(
            FabricRow(
                row_index=index,
                op="cable",
                target=f"{dev_a}:{port_a}",
                detail=f"{dev_b}:{port_b}",
                payload={
                    "a_device": dev_a,
                    "a_interface": port_a,
                    "b_device": dev_b,
                    "b_interface": port_b,
                },
            )
        )
    return rows


def _address_row(index: int, op: str, entry: Any, namespace: str) -> FabricRow:
    """One ``{device, interface, address}`` record, from loopbacks or p2p."""
    if not isinstance(entry, dict):
        return FabricRow(index, op, str(entry), parse_error="not a mapping")
    device = str(entry.get("device", "")).strip()
    interface = str(entry.get("interface", "")).strip()
    address = str(entry.get("address", "")).strip()
    missing = [
        label
        for label, value in (("device", device), ("interface", interface), ("address", address))
        if not value
    ]
    error = f"missing {', '.join(missing)}" if missing else None
    if not error:
        try:
            ipaddress.ip_interface(address)
        except ValueError:
            error = f"'{address}' is not a valid address"
    return FabricRow(
        row_index=index,
        op="address",
        target=f"{device}:{interface}" if device and interface else (device or "?"),
        detail=address,
        payload={
            "device": device,
            "interface": interface,
            "address": address,
            "namespace": namespace,
        },
        parse_error=error,
    )


def _parse_addressing(doc: dict[str, Any]) -> list[FabricRow]:
    """Prefixes first: an address whose parent prefix is absent is rejected by
    Nautobot 2.x, and that failure reads as "the address is bad"."""
    namespace = str((doc.get("meta") or {}).get("namespace") or DEFAULT_NAMESPACE)
    rows: list[FabricRow] = []
    index = 0

    for entry in doc.get("prefixes") or []:
        index += 1
        if isinstance(entry, str):
            entry = {"prefix": entry}
        if not isinstance(entry, dict):
            rows.append(FabricRow(index, "prefix", str(entry), parse_error="not a mapping"))
            continue
        prefix = str(entry.get("prefix", "")).strip()
        error = None
        if not prefix:
            error = "missing prefix"
        else:
            try:
                ipaddress.ip_network(prefix, strict=False)
            except ValueError:
                error = f"'{prefix}' is not a valid prefix"
        rows.append(
            FabricRow(
                row_index=index,
                op="prefix",
                target=prefix or "?",
                detail=str(entry.get("description") or ""),
                payload={
                    "prefix": prefix,
                    "description": str(entry.get("description") or ""),
                    "namespace": namespace,
                },
                parse_error=error,
            )
        )

    for section in ("loopbacks", "point_to_point"):
        for entry in doc.get(section) or []:
            index += 1
            rows.append(_address_row(index, "address", entry, namespace))
    return rows


def _parse_intent(doc: dict[str, Any]) -> list[FabricRow]:
    """Roles before contexts: a role-scoped context needs the role to exist."""
    rows: list[FabricRow] = []
    index = 0

    roles = doc.get("roles") or {}
    if roles and not isinstance(roles, dict):
        raise FabricParseError("'roles:' must be a mapping of device -> role name")
    for device, role in roles.items():
        index += 1
        role_name = str(role or "").strip()
        rows.append(
            FabricRow(
                row_index=index,
                op="role",
                target=str(device).strip(),
                detail=role_name,
                payload={"device": str(device).strip(), "role": role_name},
                parse_error=None if role_name else "missing role name",
            )
        )

    contexts = doc.get("config_contexts") or []
    if contexts and not isinstance(contexts, list):
        raise FabricParseError("'config_contexts:' must be a list")
    for entry in contexts:
        index += 1
        if not isinstance(entry, dict):
            rows.append(FabricRow(index, "config_context", str(entry), parse_error="not a mapping"))
            continue
        name = str(entry.get("name", "")).strip()
        scope = entry.get("scope") or {}
        data = entry.get("data")
        error = None
        if not name:
            error = "missing name"
        elif not isinstance(scope, dict):
            error = "'scope' must be a mapping"
        elif not isinstance(data, dict) or not data:
            error = "'data' must be a non-empty mapping"
        scope_role = str(scope.get("role", "")).strip() if isinstance(scope, dict) else ""
        scope_location = str(scope.get("location", "")).strip() if isinstance(scope, dict) else ""
        rows.append(
            FabricRow(
                row_index=index,
                op="config_context",
                target=name or "?",
                # The scope is the whole teaching point of this artifact, so it
                # is what the preview shows rather than a key count.
                detail=" · ".join(p for p in (scope_role, scope_location) if p),
                payload={
                    "name": name,
                    "role": scope_role,
                    "location": scope_location,
                    "data": data if isinstance(data, dict) else {},
                },
                parse_error=error,
            )
        )
    return rows


_PARSERS = {
    "cabling": _parse_cabling,
    "addressing": _parse_addressing,
    "intent": _parse_intent,
}


def parse_fabric_upload(
    filename: str,
    content_type: str | None,
    data: bytes,
    expected: str | None = None,
) -> tuple[str, list[FabricRow]]:
    """Parse an uploaded fabric plan into ``(artifact, rows)``.

    ``expected`` is the tab the user dropped the file on. When it disagrees
    with the file's own ``kind:``, that is an error naming both — the most
    likely mistake with three near-identical files is dropping the wrong one,
    and "0 rows found" would be a terrible way to learn that.
    """
    del content_type  # these artifacts are YAML-only; kept for call-site parity
    if not data.strip():
        raise FabricParseError("file is empty")
    try:
        doc = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise FabricParseError(f"not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise FabricParseError("expected a YAML mapping at the top level")

    artifact = detect_kind(doc)
    if expected and artifact != expected:
        raise FabricParseError(
            f"this file declares 'kind: {artifact}', but it was uploaded on the "
            f"{expected} tab — upload it there instead"
        )
    name = (filename or "").strip()
    rows = _PARSERS[artifact](doc)
    if not rows:
        raise FabricParseError(f"{name or 'file'} parsed as '{artifact}' but contains no rows")
    return artifact, rows
