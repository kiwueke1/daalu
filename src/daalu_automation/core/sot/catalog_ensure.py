"""Create the Nautobot catalog objects a device import references.

A freshly provisioned NV-CM environment ships NVIDIA reference data and no
locations at all, so the first inventory upload into any new environment fails
every row with "unknown site / device_type / role / platform" — the file is
fine, the catalog behind it is empty. Until now the only fix was a hand-run
seeding script hardcoded to one lab (``deploy/scripts/lab/``), which meant the
next environment hit the same wall.

This module closes that for good: the importer plans the catalog objects the
upload needs but Nautobot lacks, shows them in the dry-run preview, and creates
them on apply. Nothing is created without the operator seeing the list first —
that preview is what keeps a typo'd role from quietly becoming a real one.

Three things are less obvious than they look:

* **Locations need a tree.** The bundled schema enforces a location-type chain
  above Site (Provider → Region → Site) and Nautobot rejects a Site with no
  parent. We walk the chain from the ``Site`` type upward and create top-down,
  reusing an existing ancestor when there is exactly one candidate at that
  level rather than inventing a duplicate.
* **Device types need a manufacturer**, which a device file never names — they
  get filed under a placeholder one.
* **IPs need a parent prefix.** Nautobot 2.x refuses an IP address whose
  namespace has no covering prefix, and that failure is what leaves devices
  created with no primary IP (reported as a row warning, not an error). The
  parent prefix of every row's ``primary_ip`` is part of the plan for exactly
  that reason.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Device types must name a manufacturer and a device file never does. Anything
# auto-created lands here so it is obvious in Nautobot which rows Daalu filled
# in on the operator's behalf.
PLACEHOLDER_MANUFACTURER = "Generic"


def slugify(value: str) -> str:
    """``"Cumulus Linux"`` → ``"cumulus-linux"``."""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


@dataclass
class CatalogPlan:
    """Catalog objects the upload references that Nautobot doesn't have yet.

    Names are kept in the operator's own spelling and in file order, because
    this list is rendered straight back to them in the preview.
    """

    sites: list[str] = field(default_factory=list)
    device_types: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    prefixes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.sites
            or self.device_types
            or self.roles
            or self.platforms
            or self.prefixes
        )

    @property
    def total(self) -> int:
        return (
            len(self.sites)
            + len(self.device_types)
            + len(self.roles)
            + len(self.platforms)
            + len(self.prefixes)
        )

    def has(self, kind: str, name: str) -> bool:
        """Is ``name`` planned for creation under ``kind``? Case-insensitive."""
        wanted = (name or "").strip().lower()
        if not wanted:
            return False
        return any(n.strip().lower() == wanted for n in getattr(self, kind, []))


@dataclass
class CatalogEnsureResult:
    # Human-readable labels of what was actually created, for the run report.
    created: list[str] = field(default_factory=list)
    # Creation failures. Never raised: a catalog object we couldn't create
    # simply leaves its rows failing the way they already did, and the
    # operator gets told why instead of getting a 500 for the whole batch.
    errors: list[str] = field(default_factory=list)


def parent_prefix(primary_ip: str) -> str | None:
    """``"172.20.30.11/24"`` → ``"172.20.30.0/24"``; ``None`` if unparseable.

    A bare address with no mask parses as a host route and yields its own /32
    (or /128), which is a perfectly legal Nautobot prefix — so it still gets
    planned rather than being dropped.
    """
    try:
        iface = ipaddress.ip_interface(primary_ip.strip())
    except ValueError:
        return None
    return str(iface.network)


def _dedupe(names: Iterable[str]) -> list[str]:
    """First spelling wins, case-insensitively; order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in names:
        name = (raw or "").strip()
        key = name.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def plan_catalog_additions(
    rows: Iterable[Any],
    *,
    sites: dict[str, str],
    device_types: dict[str, str],
    roles: dict[str, str],
    platforms: dict[str, str],
    prefixes: set[str],
) -> CatalogPlan:
    """Diff the upload's catalog references against what Nautobot already has.

    ``sites`` / ``device_types`` / ``roles`` / ``platforms`` are the
    lower-cased name → UUID indexes the importer already builds; ``prefixes``
    is the set of prefix strings Nautobot holds. Rows that failed to parse
    contribute nothing — their names are not trustworthy enough to create
    objects from.
    """
    missing_sites: list[str] = []
    missing_types: list[str] = []
    missing_roles: list[str] = []
    missing_platforms: list[str] = []
    missing_prefixes: list[str] = []

    for row in rows:
        if getattr(row, "parse_error", None):
            continue
        if row.site and row.site.lower() not in sites:
            missing_sites.append(row.site)
        if row.device_type and row.device_type.lower() not in device_types:
            missing_types.append(row.device_type)
        if row.role and row.role.lower() not in roles:
            missing_roles.append(row.role)
        if row.platform and row.platform.lower() not in platforms:
            missing_platforms.append(row.platform)
        cidr = parent_prefix(row.primary_ip or "")
        if cidr and cidr not in prefixes:
            missing_prefixes.append(cidr)

    return CatalogPlan(
        sites=_dedupe(missing_sites),
        device_types=_dedupe(missing_types),
        roles=_dedupe(missing_roles),
        platforms=_dedupe(missing_platforms),
        prefixes=_dedupe(missing_prefixes),
    )


# ── Nautobot reads ───────────────────────────────────────────────────────


async def _list(client: httpx.AsyncClient, path: str) -> list[dict]:
    """GET a list endpoint, tolerating the versions that don't have it.

    Filtering happens client-side throughout this module: the NV-CM-bundled
    Nautobot 400s on some ``?field=`` filters (``/api/extras/custom-fields/
    ?key=`` is the known one), and a 400 read as "not found" would make us
    re-create objects that already exist. These lists are a few hundred rows
    at most.
    """
    try:
        resp = await client.get(path, params={"limit": 500})
    except httpx.HTTPError as e:
        logger.warning(
            "sot_catalog.list_failed", extra={"path": path, "error": str(e)}
        )
        return []
    if resp.status_code != 200:
        return []
    return (resp.json() or {}).get("results") or []


def _matches(obj: dict, name: str, *keys: str) -> bool:
    wanted = name.strip().lower()
    for key in keys:
        value = (obj.get(key) or "").strip()
        if not value:
            continue
        if value.lower() == wanted or slugify(value) == slugify(name):
            return True
    return False


async def existing_prefixes(client: httpx.AsyncClient) -> set[str]:
    """Every prefix Nautobot holds, as ``"10.0.0.0/24"`` strings."""
    out: set[str] = set()
    for obj in await _list(client, "/api/ipam/prefixes/"):
        value = obj.get("prefix") or obj.get("display")
        if value:
            out.add(str(value).strip())
    return out


# ── Nautobot writes ──────────────────────────────────────────────────────


async def _create(
    client: httpx.AsyncClient,
    result: CatalogEnsureResult,
    *,
    label: str,
    path: str,
    body: dict,
) -> str | None:
    try:
        resp = await client.post(path, json=body)
    except httpx.HTTPError as e:
        result.errors.append(f"could not create {label}: {e}")
        return None
    if resp.status_code not in (200, 201):
        # A concurrent importer may have won the race; that is success, but we
        # have no id to hand back — the caller re-reads the catalog anyway.
        if "already exists" in resp.text:
            return None
        result.errors.append(
            f"could not create {label}: HTTP {resp.status_code} — {resp.text[:160]}"
        )
        return None
    result.created.append(label)
    logger.info("sot_catalog.created", extra={"object": label})
    try:
        return str((resp.json() or {}).get("id") or "") or None
    except ValueError:
        return None


async def _ensure_location(
    client: httpx.AsyncClient, site: str, result: CatalogEnsureResult
) -> None:
    """Create ``site`` as a Site location, plus whatever ancestors it needs.

    The chain isn't hardcoded because it belongs to the chart: walk up from the
    Site location type through each ``parent`` until one has none, then create
    root-down. Skipping this is what produces "A Location of type Site must
    have a parent Location", which surfaces to the operator as the far less
    useful "unknown site".
    """
    locations = await _list(client, "/api/dcim/locations/")
    if any(_matches(loc, site, "name", "slug") for loc in locations):
        return

    types = await _list(client, "/api/dcim/location-types/")
    if not types:
        result.errors.append(
            f"could not create site '{site}': this Nautobot defines no location "
            "types — add one in Organization → Location Types first"
        )
        return
    by_id = {t["id"]: t for t in types}
    site_type = next((t for t in types if (t.get("name") or "").lower() == "site"), None)
    if site_type is None:
        # No "Site" type: use a leaf type (one nothing else parents), which is
        # the closest equivalent in a custom hierarchy.
        parented = {
            (t.get("parent") or {}).get("id") for t in types if t.get("parent")
        }
        site_type = next((t for t in types if t["id"] not in parented), types[0])

    chain = [site_type]
    seen = {site_type["id"]}
    while (parent := (chain[-1].get("parent") or {})) and parent.get("id") in by_id:
        if parent["id"] in seen:  # defensive: a self-referencing type
            break
        seen.add(parent["id"])
        chain.append(by_id[parent["id"]])

    parent_id: str | None = None
    for location_type in reversed(chain):
        is_site = location_type["id"] == site_type["id"]
        type_name = location_type.get("name") or "location"
        candidates = [
            loc
            for loc in locations
            if (loc.get("location_type") or {}).get("id") == location_type["id"]
        ]
        if not is_site and len(candidates) == 1:
            # Exactly one location at this level: unambiguous, so hang the new
            # site off the tree the operator already has rather than growing a
            # parallel one beside it.
            parent_id = str(candidates[0]["id"])
            continue
        name = site if is_site else f"{site}-{slugify(type_name)}"
        existing = next((loc for loc in candidates if _matches(loc, name, "name")), None)
        if existing is not None:
            parent_id = str(existing["id"])
            continue
        body: dict[str, Any] = {
            "name": name,
            "location_type": location_type["id"],
            "status": {"name": "Active"},
        }
        if parent_id:
            body["parent"] = parent_id
        created = await _create(
            client,
            result,
            label=f"site '{name}'" if is_site else f"{type_name} '{name}'",
            path="/api/dcim/locations/",
            body=body,
        )
        if created is None:
            # Without this level the ones below it cannot be placed; the rows
            # keep their "unknown site" error and the reason is in errors.
            return
        parent_id = created


async def _ensure_manufacturer(
    client: httpx.AsyncClient, result: CatalogEnsureResult
) -> str | None:
    manufacturers = await _list(client, "/api/dcim/manufacturers/")
    match = next(
        (m for m in manufacturers if _matches(m, PLACEHOLDER_MANUFACTURER, "name")),
        None,
    )
    if match is not None:
        return str(match["id"])
    return await _create(
        client,
        result,
        label=f"manufacturer '{PLACEHOLDER_MANUFACTURER}'",
        path="/api/dcim/manufacturers/",
        body={"name": PLACEHOLDER_MANUFACTURER},
    )


async def _roles_path(client: httpx.AsyncClient) -> str:
    """``extras/roles`` since Nautobot 2.0, ``dcim/device-roles`` before it."""
    try:
        resp = await client.get("/api/extras/roles/", params={"limit": 1})
        if resp.status_code != 404:
            return "/api/extras/roles/"
    except httpx.HTTPError:
        pass
    return "/api/dcim/device-roles/"


async def ensure_catalog(
    client: httpx.AsyncClient, plan: CatalogPlan
) -> CatalogEnsureResult:
    """Create everything in ``plan``, best-effort.

    Never raises: each object is independent, and a failure to create one has
    to leave the rest of the import working. The caller re-reads the catalog
    afterwards rather than trusting the ids returned here, so a partial run
    resolves whatever did land.
    """
    result = CatalogEnsureResult()

    for site in plan.sites:
        await _ensure_location(client, site, result)

    if plan.device_types:
        manufacturer_id = await _ensure_manufacturer(client, result)
        existing = await _list(client, "/api/dcim/device-types/")
        for model in plan.device_types:
            if any(t for t in existing if _matches(t, model, "model", "slug")):
                continue
            if manufacturer_id is None:
                result.errors.append(
                    f"could not create device type '{model}': no manufacturer to file it under"
                )
                continue
            await _create(
                client,
                result,
                label=f"device type '{model}'",
                path="/api/dcim/device-types/",
                body={
                    "model": model,
                    "manufacturer": manufacturer_id,
                    "u_height": 1,
                    "comments": "Created by Daalu from a device import.",
                },
            )

    if plan.roles:
        path = await _roles_path(client)
        existing = await _list(client, path)
        for role in plan.roles:
            if any(r for r in existing if _matches(r, role, "name", "slug")):
                continue
            body: dict[str, Any] = {"name": role}
            if path.startswith("/api/extras/"):
                # 2.x roles are shared across models and are only assignable to
                # a device once dcim.device is in their content types.
                body["content_types"] = ["dcim.device"]
            await _create(
                client, result, label=f"role '{role}'", path=path, body=body
            )

    if plan.platforms:
        existing = await _list(client, "/api/dcim/platforms/")
        for platform in plan.platforms:
            if any(p for p in existing if _matches(p, platform, "name", "slug")):
                continue
            await _create(
                client,
                result,
                label=f"platform '{platform}'",
                path="/api/dcim/platforms/",
                body={"name": platform},
            )

    if plan.prefixes:
        held = await existing_prefixes(client)
        for prefix in plan.prefixes:
            if prefix in held:
                continue
            await _create(
                client,
                result,
                label=f"prefix {prefix}",
                path="/api/ipam/prefixes/",
                body={
                    "prefix": prefix,
                    "namespace": {"name": "Global"},
                    "status": {"name": "Active"},
                    "description": "Created by Daalu from a device import.",
                },
            )

    return result
