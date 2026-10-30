"""Resolve and apply a parsed fabric plan against a tenant's Nautobot.

Two phases, the same split the device import uses:

* :func:`resolve_rows` — read-only. Every row is checked against what Nautobot
  already holds and comes back ``valid`` / ``exists`` / ``error``. This is the
  dry-run, and it is the whole value of the preview: it is where you find out
  that ``leaf1:swp1`` is already cabled somewhere else *before* you write.
* :func:`apply_rows` — the writes, in row order, each one independent. A row
  that fails is reported and the run continues, because thirty-one good cables
  should not be lost to one bad one.

Everything is idempotent by lookup: re-running a plan after a partial failure
re-creates only what is genuinely absent. That matters more here than for
devices — a fabric plan is re-applied every time the lab is rebuilt.

Nautobot payload shapes below are the ones verified against the NV-CM-bundled
Nautobot 2.4 (see ``sot_devices._create_one_device`` for the same idioms):
statuses go in as bare name strings, namespaces as ``{"name": ...}``, and the
IP-to-interface link is its own m2m object rather than a field on either side.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .catalog_ensure import _roles_path
from .fabric_import import CABLE_STATUS, FabricRow

logger = logging.getLogger(__name__)

#: Weight for a Daalu-loaded config context. Above the vendor defaults (1000)
#: so a fabric plan's intent wins over the bundled baseline, which is what the
#: user means by uploading it.
CONTEXT_WEIGHT = 1500


@dataclass
class FabricRowResult:
    """One row's outcome, in both phases.

    ``status`` is ``valid`` (would be created), ``exists`` (already correct,
    will be skipped), ``created`` (written this run) or ``error``.
    """

    row: int
    op: str
    target: str
    detail: str
    status: str
    error: str | None = None
    object_id: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class FabricIndex:
    """Everything the resolver needs to look up, read once per run."""

    devices: dict[str, str] = field(default_factory=dict)
    #: ``(device_lower, interface_lower)`` → ``{"id", "cable"}``
    interfaces: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    #: device id → role name, so an already-correct role is a skip not a write
    device_roles: dict[str, str] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    locations: dict[str, str] = field(default_factory=dict)
    prefixes: dict[str, str] = field(default_factory=dict)
    addresses: dict[str, str] = field(default_factory=dict)
    #: address id -> interface ids it is already assigned to. An address can
    #: exist while sitting on nothing, so holding the address alone is not
    #: enough to call a row done.
    assignments: dict[str, set[str]] = field(default_factory=dict)
    contexts: dict[str, str] = field(default_factory=dict)
    #: interface id → the id of the cable terminating on it
    cabled: dict[str, str] = field(default_factory=dict)
    #: unordered interface-id pairs that already have a cable between them
    cable_pairs: set[frozenset[str]] = field(default_factory=set)

    def device_id(self, name: str) -> str | None:
        return self.devices.get(name.strip().lower())

    def interface(self, device: str, name: str) -> dict[str, Any] | None:
        return self.interfaces.get((device.strip().lower(), name.strip().lower()))


#: Rows per page when walking a list endpoint. Nautobot caps `limit` server-side
#: (commonly at 1000), so this is a request size, not a total.
PAGE_SIZE = 500


async def _list_all(client: httpx.AsyncClient, path: str) -> list[dict[str, Any]]:
    """Every row at ``path``, following Nautobot's ``next`` links.

    The catalog helper in :mod:`catalog_ensure` reads a single 500-row page,
    which is right for sites and roles. It is wrong here: a 32-switch fabric
    has thousands of interfaces, and a truncated index does not fail loudly —
    it reports the interfaces it never read as "unknown interface", i.e. it
    blames the plan for the reader's cap.
    """
    out: list[dict[str, Any]] = []
    params: dict[str, Any] | None = {"limit": PAGE_SIZE}
    url = path
    while url:
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            logger.warning("sot_fabric.list_failed", extra={"path": url, "error": str(exc)})
            return out
        if resp.status_code != 200:
            return out
        body = resp.json() or {}
        out.extend(body.get("results") or [])
        # `next` comes back absolute and already carries offset/limit.
        url = body.get("next") or ""
        params = None
    return out


def _termination_id(cable: dict[str, Any], side: str) -> str | None:
    """The interface id on one end of a cable, however this Nautobot spells it.

    2.x returns a flat ``termination_a_id``; some versions and some
    ``?depth=`` settings nest it as ``termination_a: {"id": ...}`` instead.
    Reading only one spelling would silently produce an empty
    ``cable_pairs``, which turns "already cabled" into "will create" and makes
    a re-upload look like it has 32 cables to add.
    """
    flat = cable.get(f"termination_{side}_id")
    if flat:
        return str(flat)
    nested = cable.get(f"termination_{side}")
    if isinstance(nested, dict) and nested.get("id"):
        return str(nested["id"])
    return None


async def load_index(client: httpx.AsyncClient, artifact: str) -> FabricIndex:
    """Read the lookups ``artifact`` needs. Devices are always needed."""
    index = FabricIndex()

    for obj in await _list_all(client, "/api/dcim/devices/"):
        name = (obj.get("name") or "").strip()
        if name:
            index.devices[name.lower()] = str(obj["id"])
            role = obj.get("role") or {}
            role_name = role.get("name") if isinstance(role, dict) else None
            if role_name:
                index.device_roles[str(obj["id"])] = str(role_name)

    if artifact in ("cabling", "addressing"):
        # Interfaces carry their own device reference, so one list call covers
        # every device — and the `cable` field is what makes an occupied
        # termination visible in the dry-run.
        by_id = {v: k for k, v in index.devices.items()}
        for obj in await _list_all(client, "/api/dcim/interfaces/"):
            device = obj.get("device") or {}
            device_id = str(device.get("id") or "") if isinstance(device, dict) else ""
            device_name = (
                (device.get("name") or "").strip().lower() if isinstance(device, dict) else ""
            ) or by_id.get(device_id, "")
            name = (obj.get("name") or "").strip()
            if not device_name or not name:
                continue
            cable = obj.get("cable")
            cable_id = str(cable.get("id")) if isinstance(cable, dict) and cable.get("id") else None
            index.interfaces[(device_name, name.lower())] = {
                "id": str(obj["id"]),
                "cable": cable_id,
            }
            if cable_id:
                index.cabled[str(obj["id"])] = cable_id

    if artifact == "cabling":
        for obj in await _list_all(client, "/api/dcim/cables/"):
            a_id = _termination_id(obj, "a")
            b_id = _termination_id(obj, "b")
            if a_id and b_id:
                index.cable_pairs.add(frozenset({a_id, b_id}))

    if artifact == "addressing":
        for obj in await _list_all(client, "/api/ipam/prefixes/"):
            value = obj.get("prefix") or obj.get("display")
            if value:
                index.prefixes[str(value).strip()] = str(obj["id"])
        for obj in await _list_all(client, "/api/ipam/ip-addresses/"):
            value = obj.get("address") or obj.get("display")
            if value:
                index.addresses[str(value).strip()] = str(obj["id"])
        for obj in await _list_all(client, "/api/ipam/ip-address-to-interface/"):
            ip = obj.get("ip_address") or {}
            iface = obj.get("interface") or {}
            ip_id = str(ip.get("id") or "") if isinstance(ip, dict) else str(ip or "")
            iface_id = str(iface.get("id") or "") if isinstance(iface, dict) else str(iface or "")
            if ip_id and iface_id:
                index.assignments.setdefault(ip_id, set()).add(iface_id)

    if artifact == "intent":
        for obj in await _list_all(client, await _roles_path(client)):
            name = (obj.get("name") or "").strip()
            if name:
                index.roles[name.lower()] = str(obj["id"])
        for obj in await _list_all(client, "/api/dcim/locations/"):
            name = (obj.get("name") or "").strip()
            if name:
                index.locations[name.lower()] = str(obj["id"])
        for obj in await _list_all(client, "/api/extras/config-contexts/"):
            name = (obj.get("name") or "").strip()
            if name:
                index.contexts[name.lower()] = str(obj["id"])

    return index


def _planned_interfaces(rows: list[FabricRow]) -> set[tuple[str, str]]:
    """Interfaces this same upload will create, so a cable can reference one.

    Mirrors the device import's "will create site 'x'" behaviour: a row that
    depends on an earlier row in the same file is valid, not an error.
    """
    return {
        (r.payload["device"].lower(), r.payload["name"].lower())
        for r in rows
        if r.op == "interface" and not r.parse_error and r.payload.get("name")
    }


def _planned_roles(rows: list[FabricRow]) -> set[str]:
    return {
        r.payload["role"].lower()
        for r in rows
        if r.op == "role" and not r.parse_error and r.payload.get("role")
    }


def resolve_rows(rows: list[FabricRow], index: FabricIndex) -> list[FabricRowResult]:
    """Check every row against Nautobot without writing anything.

    Which lookups ``index`` holds is decided by :func:`load_index`; resolution
    itself is purely per-row, so no artifact argument is needed here.
    """
    planned_ifaces = _planned_interfaces(rows)
    planned_roles = _planned_roles(rows)
    results: list[FabricRowResult] = []

    for row in rows:
        if row.parse_error:
            results.append(
                FabricRowResult(
                    row=row.row_index,
                    op=row.op,
                    target=row.target,
                    detail=row.detail,
                    status="error",
                    error=row.parse_error,
                )
            )
            continue

        errors: list[str] = []
        warnings: list[str] = []
        status = "valid"
        object_id: str | None = None
        p = row.payload

        if row.op == "interface":
            if index.device_id(p["device"]) is None:
                errors.append(f"unknown device '{p['device']}'")
            elif (existing := index.interface(p["device"], p["name"])) is not None:
                status, object_id = "exists", existing["id"]
                warnings.append("interface already present")

        elif row.op == "cable":
            ends: list[dict[str, Any] | None] = []
            for side in ("a", "b"):
                dev, iface = p[f"{side}_device"], p[f"{side}_interface"]
                if index.device_id(dev) is None:
                    errors.append(f"unknown device '{dev}'")
                    ends.append(None)
                elif (existing := index.interface(dev, iface)) is not None:
                    ends.append(existing)
                elif (dev.lower(), iface.lower()) in planned_ifaces:
                    warnings.append(f"will create interface {dev}:{iface}")
                    ends.append(None)
                else:
                    errors.append(f"unknown interface '{dev}:{iface}'")
                    ends.append(None)
            end_a, end_b = ends if len(ends) == 2 else (None, None)
            if not errors and end_a is not None and end_b is not None:
                pair = frozenset({end_a["id"], end_b["id"]})
                if pair in index.cable_pairs:
                    status = "exists"
                    warnings.append("cable already present")
                else:
                    # The constraint that makes a source of truth a source of
                    # truth: a termination holds exactly one cable. Surface it
                    # here rather than letting Nautobot 400 at apply time.
                    for side, end in zip(("a", "b"), ends, strict=True):
                        if end and end.get("cable"):
                            errors.append(
                                f"{p[f'{side}_device']}:{p[f'{side}_interface']} already has a "
                                "cable — a termination holds exactly one"
                            )

        elif row.op == "prefix":
            if p["prefix"] in index.prefixes:
                status, object_id = "exists", index.prefixes[p["prefix"]]
                warnings.append("prefix already present")

        elif row.op == "address":
            if index.device_id(p["device"]) is None:
                errors.append(f"unknown device '{p['device']}'")
            elif index.interface(p["device"], p["interface"]) is None:
                errors.append(
                    f"unknown interface '{p['device']}:{p['interface']}' — "
                    "upload the cabling file first"
                )
            existing_ip = index.addresses.get(p["address"])
            if existing_ip and not errors:
                object_id = existing_ip
                iface = index.interface(p["device"], p["interface"])
                if iface and iface["id"] in index.assignments.get(existing_ip, set()):
                    status = "exists"
                    warnings.append("address already on this interface")
                else:
                    # The address object exists but sits on nothing (or on
                    # something else). Re-using it and assigning is the fix, so
                    # this row still has work to do.
                    warnings.append("address exists; will assign it to this interface")

        elif row.op == "role":
            if index.device_id(p["device"]) is None:
                errors.append(f"unknown device '{p['device']}'")
            else:
                current = index.device_roles.get(index.device_id(p["device"]) or "")
                if current and current.lower() == p["role"].lower():
                    status = "exists"
                    warnings.append(f"already {p['role']}")
                elif p["role"].lower() not in index.roles:
                    warnings.append(f"will create role '{p['role']}'")
                if current and current.lower() != p["role"].lower() and status != "exists":
                    warnings.append(f"was '{current}'")

        elif row.op == "config_context":
            if p["role"] and p["role"].lower() not in index.roles:
                if p["role"].lower() in planned_roles:
                    warnings.append(f"will create role '{p['role']}'")
                else:
                    errors.append(f"unknown role '{p['role']}'")
            if p["location"] and p["location"].lower() not in index.locations:
                errors.append(f"unknown location '{p['location']}'")
            if p["name"].lower() in index.contexts:
                status, object_id = "exists", index.contexts[p["name"].lower()]
                warnings.append("context already present; data will be updated")

        results.append(
            FabricRowResult(
                row=row.row_index,
                op=row.op,
                target=row.target,
                detail=row.detail,
                status="error" if errors else status,
                error="; ".join(errors) or None,
                object_id=object_id,
                warnings=warnings,
            )
        )
    return results


async def _post(client: httpx.AsyncClient, path: str, body: dict) -> tuple[str | None, str | None]:
    """POST and return ``(id, error)`` — never raises."""
    try:
        resp = await client.post(path, json=body)
    except httpx.HTTPError as exc:
        return None, str(exc)
    if resp.status_code not in (200, 201):
        return None, f"HTTP {resp.status_code} — {resp.text[:200]}"
    try:
        return str((resp.json() or {}).get("id") or "") or None, None
    except ValueError:
        return None, None


async def _ensure_role(
    client: httpx.AsyncClient, index: FabricIndex, name: str
) -> tuple[str | None, str | None]:
    """Find or create a device role, caching the id back into the index.

    The fabric plan deliberately restates roles as the names the template tree
    uses (``Leaf-Common``), which are usually absent from a Nautobot seeded with
    ``spine``/``leaf`` — so creating them is the normal path, not the exception.
    """
    if (found := index.roles.get(name.lower())) is not None:
        return found, None
    path = await _roles_path(client)
    body: dict[str, Any] = {"name": name}
    if path == "/api/extras/roles/":
        body["content_types"] = ["dcim.device"]
    role_id, error = await _post(client, path, body)
    if role_id:
        index.roles[name.lower()] = role_id
    return role_id, error


async def apply_rows(
    client: httpx.AsyncClient,
    rows: list[FabricRow],
    results: list[FabricRowResult],
    index: FabricIndex,
) -> list[FabricRowResult]:
    """Write every resolvable row, updating ``index`` as objects appear.

    ``results`` is the resolution from :func:`resolve_rows`, re-used so an
    ``error`` row is skipped and an ``exists`` row is a no-op. The index is
    mutated as we go because a cable created later in this same run must find
    the interface created earlier in it.
    """
    out: list[FabricRowResult] = []

    for row, resolved in zip(rows, results, strict=True):
        if resolved.status in ("error", "exists"):
            out.append(resolved)
            continue

        p = row.payload
        error: str | None = None
        object_id: str | None = None

        if row.op == "interface":
            device_id = index.device_id(p["device"])
            object_id, error = await _post(
                client,
                "/api/dcim/interfaces/",
                {
                    "device": device_id,
                    "name": p["name"],
                    "type": p["type"],
                    "status": "Active",
                    "description": "fabric port (daalu-created)",
                },
            )
            if object_id:
                index.interfaces[(p["device"].lower(), p["name"].lower())] = {
                    "id": object_id,
                    "cable": None,
                }

        elif row.op == "cable":
            a = index.interface(p["a_device"], p["a_interface"])
            b = index.interface(p["b_device"], p["b_interface"])
            if not a or not b:
                error = "an interface this cable needs was not created"
            else:
                object_id, error = await _post(
                    client,
                    "/api/dcim/cables/",
                    {
                        "termination_a_type": "dcim.interface",
                        "termination_a_id": a["id"],
                        "termination_b_type": "dcim.interface",
                        "termination_b_id": b["id"],
                        "status": CABLE_STATUS,
                    },
                )
                if object_id:
                    a["cable"] = b["cable"] = object_id
                    index.cable_pairs.add(frozenset({a["id"], b["id"]}))

        elif row.op == "prefix":
            object_id, error = await _post(
                client,
                "/api/ipam/prefixes/",
                {
                    "prefix": p["prefix"],
                    "namespace": {"name": p["namespace"]},
                    "status": "Active",
                    "description": p["description"] or "Created by Daalu from a fabric plan.",
                },
            )
            if object_id:
                index.prefixes[p["prefix"]] = object_id

        elif row.op == "address":
            iface = index.interface(p["device"], p["interface"])
            object_id = index.addresses.get(p["address"])
            if object_id is None:
                object_id, error = await _post(
                    client,
                    "/api/ipam/ip-addresses/",
                    {
                        "address": p["address"],
                        "status": "Active",
                        "namespace": {"name": p["namespace"]},
                    },
                )
                if object_id:
                    index.addresses[p["address"]] = object_id
            if object_id and iface and not error:
                # Its own m2m object in Nautobot 2.x — an address is not a
                # field on the interface, which is the point Clip 10 makes.
                _, assign_error = await _post(
                    client,
                    "/api/ipam/ip-address-to-interface/",
                    {"ip_address": object_id, "interface": iface["id"]},
                )
                if assign_error and "already exists" not in assign_error:
                    error = f"address created but not assigned: {assign_error}"
                else:
                    index.assignments.setdefault(object_id, set()).add(iface["id"])

        elif row.op == "role":
            role_id, error = await _ensure_role(client, index, p["role"])
            device_id = index.device_id(p["device"])
            if role_id and device_id and not error:
                try:
                    resp = await client.patch(
                        f"/api/dcim/devices/{device_id}/", json={"role": role_id}
                    )
                    if resp.status_code not in (200, 201):
                        error = f"HTTP {resp.status_code} — {resp.text[:200]}"
                    else:
                        object_id = device_id
                        index.device_roles[device_id] = p["role"]
                except httpx.HTTPError as exc:
                    error = str(exc)

        elif row.op == "config_context":
            role_id = index.roles.get(p["role"].lower()) if p["role"] else None
            if p["role"] and role_id is None:
                role_id, error = await _ensure_role(client, index, p["role"])
            location_id = index.locations.get(p["location"].lower()) if p["location"] else None
            if not error:
                body: dict[str, Any] = {
                    "name": p["name"],
                    "weight": CONTEXT_WEIGHT,
                    "is_active": True,
                    "data": p["data"],
                }
                if role_id:
                    body["roles"] = [role_id]
                if location_id:
                    body["locations"] = [location_id]
                object_id, error = await _post(client, "/api/extras/config-contexts/", body)
                if object_id:
                    index.contexts[p["name"].lower()] = object_id

        out.append(
            FabricRowResult(
                row=resolved.row,
                op=resolved.op,
                target=resolved.target,
                detail=resolved.detail,
                status="error" if error else "created",
                error=error,
                object_id=object_id,
                warnings=resolved.warnings,
            )
        )
        if error:
            logger.warning(
                "sot_fabric.row_failed",
                extra={"op": row.op, "target": row.target, "error": error},
            )

    return out


def summarize(results: list[FabricRowResult]) -> dict[str, int]:
    """Counts the preview header shows. ``skipped`` is the idempotency signal."""
    return {
        "total": len(results),
        "valid": sum(1 for r in results if r.status == "valid"),
        "created": sum(1 for r in results if r.status == "created"),
        "skipped": sum(1 for r in results if r.status == "exists"),
        "errors": sum(1 for r in results if r.status == "error"),
    }


def count_by_op(results: list[FabricRowResult]) -> dict[str, int]:
    """Per-object-type counts — "71 interfaces, 32 cables" — for the summary."""
    counts: dict[str, int] = {}
    for r in results:
        if r.status in ("valid", "created", "exists"):
            counts[r.op] = counts.get(r.op, 0) + 1
    return counts
