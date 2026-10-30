"""Seed a tenant's bundled Nautobot with the catalog the lab fabric imports against.

The NV-CM-bundled Nautobot ships NVIDIA reference data — 8 device types, 100
roles, 4 platforms — and no locations at all. ``examples/fabric-plan/fabric-devices.yaml``
references lab fixtures (`lab-site`, `cumulus-vx`, `spine`/`leaf`/`server`),
so a freshly provisioned environment fails all 12 rows at dry-run with
"unknown site / device_type / role". This creates exactly what that file needs.

These are lab fixtures, not product invariants, which is why provisioning does
not create them: a real customer's sites and roles are their own. The one
exception is the ``daalu_transport`` custom field, which Daalu stamps on every
device it creates — the API ensures that itself, and it is repeated here so
this script also works against an older build.

Idempotent: every object is looked up by name first, so re-running after a
re-provision (or alongside another seeder) is safe.

Run it against the hub, which already holds the tunnel + credentials:

    kubectl exec -i -n daalu-automation deploy/daalu-api -c api -- \
        python - --tenant 00000000-0000-0000-0000-000000000010 \
        < deploy/scripts/lab/seed-nautobot-lab-catalog.py
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

# The site every lab device is homed to. The bundled schema enforces a
# location-type chain above Site (Provider → Region → Site), and Nautobot
# rejects a Site with no parent — so the ancestors are created too, named
# after the lab. Their names only ever show up in Nautobot's location tree.
SITE = "lab-site"
ANCESTOR_NAMES = {"Provider": "daalu-lab", "Region": "lab"}
MANUFACTURER = "NVIDIA"
DEVICE_TYPES = ["cumulus-vx", "gpu-server", "storage-server"]
ROLES = ["spine", "leaf", "server"]
# "Cumulus Linux" already exists in the seed data and resolves from the
# hyphenated form via the catalog index's slugified alias. Only plain Linux is
# genuinely absent. Named "Linux" so it matches `linux` on any build.
PLATFORMS = ["Linux"]
# Nautobot 2.x refuses an IP with no parent prefix in its namespace, and that
# failure is what leaves a device created with no primary IP.
PREFIX = "172.20.20.0/24"


async def _get(client, path, **params):
    r = await client.get(path, params={"limit": 250, **params})
    if r.status_code != 200:
        return []
    return (r.json() or {}).get("results") or []


async def _ensure(client, label, path, lookup, body):
    """Create ``body`` at ``path`` unless ``lookup`` already matches something.

    Falls back to matching client-side when the server rejects the filter:
    ``/api/extras/custom-fields/?key=`` 400s on the bundled Nautobot, and
    treating that as "not found" would re-POST an object that already exists.
    """
    existing = await _get(client, path, **lookup)
    if not existing and lookup:
        field, wanted = next(iter(lookup.items()))
        existing = [o for o in await _get(client, path) if o.get(field) == wanted]
    if existing:
        print(f"  = {label} (exists)")
        return existing[0]["id"]
    r = await client.post(path, json=body)
    if r.status_code not in (200, 201):
        print(f"  ! {label} FAILED HTTP {r.status_code}: {r.text[:200]}")
        return None
    print(f"  + {label} created")
    return r.json()["id"]


async def _ensure_site_chain(client) -> str | None:
    """Create ``SITE`` plus whatever ancestors its location type demands.

    The chain isn't hardcoded because it's the chart's to define: walk up from
    the Site location type through each ``parent`` until one has none, then
    create top-down. A Site whose type has a parent type is rejected outright
    ("A Location of type Site must have a parent Location"), and the resulting
    dry-run just says "unknown site", which points at the device file rather
    than at the missing tree.
    """
    types = await _get(client, "/api/dcim/location-types/")
    by_id = {t["id"]: t for t in types}
    site_type = next((t for t in types if t.get("name") == "Site"), None)
    if site_type is None:
        print("  ! no 'Site' location type in this Nautobot")
        return None

    # Site first, then each ancestor — reversed below to create root-down.
    chain = [site_type]
    seen = {site_type["id"]}
    while (parent := chain[-1].get("parent")) and parent.get("id") in by_id:
        if parent["id"] in seen:  # defensive: a self-referencing type
            break
        seen.add(parent["id"])
        chain.append(by_id[parent["id"]])

    parent_id: str | None = None
    for lt in reversed(chain):
        type_name = lt.get("name") or ""
        name = SITE if type_name == "Site" else ANCESTOR_NAMES.get(type_name, type_name)
        body = {
            "name": name,
            "location_type": lt["id"],
            "status": {"name": "Active"},
        }
        if parent_id:
            body["parent"] = parent_id
        parent_id = await _ensure(
            client, f"{name} ({type_name})", "/api/dcim/locations/", {"name": name}, body
        )
        if parent_id is None:
            return None
    return parent_id


async def main(tenant_id: uuid.UUID) -> None:
    from daalu_automation.api.routers.sot_devices import _nautobot_client
    from daalu_automation.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        async with await _nautobot_client(db, tenant_id) as c:
            print("site:")
            await _ensure_site_chain(c)

            print("device types:")
            mfg = await _get(c, "/api/dcim/manufacturers/", name=MANUFACTURER)
            mfg_id = mfg[0]["id"] if mfg else None
            for model in DEVICE_TYPES:
                await _ensure(
                    c,
                    model,
                    "/api/dcim/device-types/",
                    {"model": model},
                    {"model": model, "manufacturer": mfg_id, "u_height": 1},
                )

            print("roles:")
            for role in ROLES:
                await _ensure(
                    c,
                    role,
                    "/api/extras/roles/",
                    {"name": role},
                    {"name": role, "content_types": ["dcim.device"]},
                )

            print("platforms:")
            for plat in PLATFORMS:
                await _ensure(
                    c, plat, "/api/dcim/platforms/", {"name": plat}, {"name": plat}
                )

            print("prefix:")
            await _ensure(
                c,
                PREFIX,
                "/api/ipam/prefixes/",
                {"prefix": PREFIX},
                {
                    "prefix": PREFIX,
                    "namespace": {"name": "Global"},
                    "status": {"name": "Active"},
                },
            )

            print("custom field:")
            await _ensure(
                c,
                "daalu_transport",
                "/api/extras/custom-fields/",
                {"key": "daalu_transport"},
                {
                    "key": "daalu_transport",
                    "label": "Daalu Transport",
                    "type": "text",
                    "content_types": ["dcim.device"],
                },
            )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    args = ap.parse_args()
    asyncio.run(main(uuid.UUID(args.tenant)))
