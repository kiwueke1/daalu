"""HTTP routes for device management — list / create / read / update-intent / delete.

These are thin proxies through the :class:`SourceOfTruth` abstraction so the
frontend never has to talk to Nautobot directly. Tenants don't need
Nautobot-UI access; everything they need flows through the daalu API
with the tenant's own JWT.

Read paths reuse ``NautobotSoT.list_devices`` / ``get_device`` /
``get_intended_config`` from PR 1.

Write paths are new in this module:

* ``POST /sot/devices`` creates a Nautobot Device row. Hits the same
  Nautobot REST surface the provisioning code uses (PR 8) — admin
  token comes from the per-tenant integration row, never the
  platform-admin token.
* ``PUT /sot/devices/{id}/intent`` updates the device's ``daalu_intent``
  Config Context via :meth:`SourceOfTruth.put_intended_config`. This is
  the path the operator's intent editor lands on.
* ``DELETE /sot/devices/{id}`` removes the device from Nautobot. The
  daalu side has nothing else to clean up — Integration rows live by
  ``device_id`` reference but a dead reference is fine; the reconciler
  just stops seeing the device on its next pass.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_admin, current_tenant_id
from daalu_automation.core.sot import (
    IntendedConfig,
    NautobotSoT,
    NautobotUnavailable,
)
from daalu_automation.core.sot.bulk_import import ParsedRow, parse_upload
from daalu_automation.core.sot.catalog_ensure import (
    CatalogPlan,
    ensure_catalog,
    existing_prefixes,
    plan_catalog_additions,
)
from daalu_automation.core.sot.catalog_ensure import slugify as _slugify
from daalu_automation.core.sot.models import DeviceFacts
from daalu_automation.core.sot.nautobot import (
    NAUTOBOT_PROVIDER,
    nautobot_client,
)
from daalu_automation.database import get_db
from daalu_automation.models import Integration, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sot/devices", tags=["sot_devices"])


# ── Schemas ──────────────────────────────────────────────────────────────


class DeviceOut(BaseModel):
    id: str
    name: str
    primary_ip: str | None = None
    platform: str
    transport: str
    tags: list[str] = Field(default_factory=list)
    # Nautobot custom_field_data — surfaces our overrides
    # (daalu_transport, ssh_user, ssh_credentials_ref, …) plus any
    # operator-defined fields. Free-form on purpose.
    extra: dict[str, Any] = Field(default_factory=dict)


class IntentOut(BaseModel):
    device_id: str
    revision: str
    transport: str
    facts: dict[str, Any]  # serialized DeviceFacts — type depends on transport
    fetched_at: datetime


class DeviceCreateIn(BaseModel):
    """Minimal payload to create a Nautobot Device row.

    The frontend resolves UUID references (site, device_type, role) on the
    customer's behalf by hitting their Nautobot directly via the proxy
    GET routes below — or, more usually, the customer picks from the
    catalogues the wizard pre-fetches. To keep v1 simple we accept the
    UUIDs directly; the wizard does the resolution before POSTing.
    """

    name: str
    primary_ip: str  # CIDR form, e.g. "10.0.0.5/24"
    site_id: str
    device_type_id: str
    device_role_id: str
    platform_id: str | None = None
    # daalu_transport custom field. Required so the device is dispatchable
    # the moment it lands — saves a follow-up edit.
    transport: str  # linux_ssh | redfish | junos | iosxr | eos


class IntentUpdateIn(BaseModel):
    # Same shape as the facts: a transport-dependent JSON blob. Validated
    # against the matching pydantic model server-side before write.
    facts: dict[str, Any]


# ── Catalog (small read-only set the wizard needs to render dropdowns) ──


class CatalogItem(BaseModel):
    id: str
    name: str
    slug: str | None = None


class CatalogOut(BaseModel):
    sites: list[CatalogItem]
    device_types: list[CatalogItem]
    device_roles: list[CatalogItem]
    platforms: list[CatalogItem]
    # Endpoints that could not be read. Without this an unreachable or
    # forbidden Nautobot returns exactly what a genuinely empty one does —
    # empty lists — and the import then blames the operator's file for
    # "unknown site/role/type" when the real fault is the connection.
    errors: list[str] = []


# ── Helpers ──────────────────────────────────────────────────────────────


ENV_QUERY = Query(
    None,
    description=(
        "NV-CM management environment slug. Each environment bundles its own "
        "Nautobot, so this selects which inventory the call reads or writes; "
        "omit for the tenant's default (primary) source of truth."
    ),
)


async def _nautobot_client(
    db: AsyncSession, tenant_id: uuid.UUID, env: str | None = None
) -> httpx.AsyncClient:
    """Build the per-tenant Nautobot HTTP client.

    Delegates to :func:`core.sot.nautobot.nautobot_client` so the REST-level
    write paths resolve credentials exactly like the SoT abstraction —
    including preferring the NV-CM-bundled Nautobot (auto-wired from the
    environment) over the standalone BYO integration. ``env`` pins the client
    to one named environment's bundled Nautobot instead.
    """
    try:
        return await nautobot_client(db, tenant_id, env)
    except NautobotUnavailable as e:
        raise HTTPException(
            409,
            "no Nautobot is wired for this tenant — provision an NV-CM "
            "environment (Managed infra → Network & servers), or connect a "
            f"bring-your-own Nautobot at /onboarding/sot ({e})",
        ) from e


def _facts_to_blob(facts: DeviceFacts) -> dict[str, Any]:
    """Round-trip the typed facts back to a JSON-serialisable dict for the API."""
    from daalu_automation.core.sot.nautobot import _dispatch_to_blob

    return _dispatch_to_blob(facts)


def _parse_intent(transport: str, raw: dict[str, Any]) -> DeviceFacts:
    """Coerce a wizard-posted intent blob to the right DeviceFacts subtype.

    Uses strict pydantic validation so malformed shapes raise instead of
    being silently dropped. NautobotSoT's _dispatch_parse is intentionally
    tolerant on the *read* path (don't poison a device's whole intent
    on one bad key) — on the *write* path through the wizard, the
    operator deserves a 422 telling them exactly what's wrong rather
    than silent data loss.
    """
    from daalu_automation.core.sot.models import (
        LinuxFacts,
        NetworkFacts,
        RedfishFacts,
    )

    if transport == "redfish":
        return RedfishFacts(**raw)
    if transport in {"junos", "iosxr", "eos"}:
        return NetworkFacts(**raw)
    return LinuxFacts(**raw)


# ── List + read ──────────────────────────────────────────────────────────


@router.get("", response_model=list[DeviceOut])
async def list_devices(
    platform: str | None = None,
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    """List every device the tenant has in their SoT.

    Optional ``?platform=linux`` filters to one bucket. Cross-tenant
    isolation is handled by the per-tenant Nautobot token — each
    tenant's token can only see their own Tenant's devices (the
    ObjectPermission constraint set up by hosted-Nautobot provisioning,
    or whatever scope the BYO operator wired).
    """
    sot = NautobotSoT(env=env)
    try:
        return await sot.list_devices(db, tenant_id, platform=platform)
    except NautobotUnavailable as e:
        raise HTTPException(409, str(e)) from e


class SotStatusOut(BaseModel):
    wired: bool
    source: str | None = None  # "nvcm" | "byo"
    reachable: bool = False
    detail: str = ""


@router.get("/_status", response_model=SotStatusOut)
async def sot_status(
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    """Whether a Nautobot is wired for this tenant, and whether it answers.

    Cheap enough for a panel to poll: one ``/api/status/`` probe through
    whatever transport the credential loader resolved (tunnel-proxied
    gateway dial for the NV-CM-bundled Nautobot, direct for BYO).
    Declared before ``/{device_id}`` so the literal path wins the match.
    """
    from daalu_automation.core.sot.nautobot import _load_credentials

    try:
        url, token, proxy_url, forward_host = await _load_credentials(
            db, tenant_id, env
        )
    except NautobotUnavailable as e:
        return SotStatusOut(wired=False, detail=str(e))
    source = "nvcm" if forward_host else "byo"
    try:
        async with await nautobot_client(db, tenant_id, env) as client:
            resp = await client.get("/api/status/")
            resp.raise_for_status()
    except Exception as e:  # noqa: BLE001 — a status probe must not 500
        return SotStatusOut(
            wired=True, source=source, reachable=False, detail=str(e)
        )
    return SotStatusOut(wired=True, source=source, reachable=True)


@router.get("/{device_id}", response_model=DeviceOut)
async def get_device(
    device_id: str,
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    sot = NautobotSoT(env=env)
    try:
        dev = await sot.get_device(db, tenant_id, device_id)
    except NautobotUnavailable as e:
        raise HTTPException(409, str(e)) from e
    if dev is None:
        raise HTTPException(404, f"device {device_id} not found")
    return dev


@router.get("/{device_id}/intent", response_model=IntentOut)
async def get_intent(
    device_id: str,
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    """Read the device's current ``daalu_intent`` Config Context.

    404 when intent is unset — that's the "operator hasn't authored
    intent for this device yet" state, distinct from "device doesn't
    exist" (which is also 404 from get_device). The wizard distinguishes
    the two by calling get_device first.
    """
    sot = NautobotSoT(env=env)
    try:
        intent = await sot.get_intended_config(db, tenant_id, device_id)
    except NautobotUnavailable as e:
        raise HTTPException(409, str(e)) from e
    if intent is None:
        raise HTTPException(404, f"device {device_id} has no daalu_intent set")
    dev = await sot.get_device(db, tenant_id, device_id)
    transport = dev.transport if dev else "unknown"
    return IntentOut(
        device_id=device_id,
        revision=intent.revision,
        transport=transport,
        facts=_facts_to_blob(intent.facts),
        fetched_at=intent.fetched_at,
    )


# ── Catalog (for the "Add device" form's dropdowns) ─────────────────────


@router.get("/_catalog/list", response_model=CatalogOut)
async def catalog(
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    """Return enough Nautobot reference data to populate the Add-Device form.

    Sites / device types / roles / platforms. Each is a small bounded
    list per tenant; we fetch all of them in one batch rather than
    paginate. If a tenant has thousands of any of these the wizard will
    need a typeahead — a follow-up.
    """
    async with await _nautobot_client(db, tenant_id, env) as client:
        out = CatalogOut(sites=[], device_types=[], device_roles=[], platforms=[])
        for endpoint, attr in [
            ("/api/dcim/locations/", "sites"),
            ("/api/dcim/device-types/", "device_types"),
            # Roles live under extras/ since Nautobot 2.0; the dcim/ path is
            # the 1.x location. Each 404s on the other version and 404 is a
            # skip, so listing both keeps the catalog working on either.
            ("/api/extras/roles/", "device_roles"),
            ("/api/dcim/roles/", "device_roles"),
            ("/api/dcim/platforms/", "platforms"),
        ]:
            try:
                resp = await client.get(endpoint, params={"limit": 250})
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                items = (resp.json() or {}).get("results") or []
                getattr(out, attr).extend(
                    CatalogItem(
                        id=str(it["id"]),
                        # Device types carry `model`, not `name` — and their
                        # `display` is "<manufacturer> <model>", which is not
                        # what an operator types in a spreadsheet.
                        name=it.get("name")
                        or it.get("model")
                        or it.get("display")
                        or str(it["id"]),
                        # 1.x only. Deliberately NOT falling back to 2.x's
                        # `natural_slug`: that is a uniquified identifier with
                        # a hash suffix (`cumulus-linux_0ec5`), so it never
                        # matches what an operator writes. The hyphenated form
                        # is recovered by slugifying the name instead — see
                        # _catalog_index.
                        slug=it.get("slug"),
                    )
                    for it in items
                )
            except httpx.HTTPError as e:
                logger.warning(
                    "sot_devices.catalog.fetch_failed",
                    extra={"endpoint": endpoint, "error": str(e)},
                )
                out.errors.append(f"{endpoint}: {e}")
        return out


# ── Create / update / delete ─────────────────────────────────────────────


async def _resolve_nautobot_tenant_id(
    db: AsyncSession, tenant_id: uuid.UUID
) -> str | None:
    integ_row = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.provider == NAUTOBOT_PROVIDER,
            )
        )
    ).scalar_one_or_none()
    if integ_row is None:
        # NV-CM-bundled Nautobot: no standalone row exists and the whole
        # install belongs to this tenant, so there is no hosted-slice
        # Nautobot-tenant to scope writes to.
        return None
    return (integ_row.config or {}).get("nautobot_tenant_id")


async def _create_one_device(
    client: httpx.AsyncClient,
    *,
    name: str,
    primary_ip: str,
    site_id: str,
    device_type_id: str,
    device_role_id: str,
    platform_id: str | None,
    transport: str,
    nautobot_tenant_id: str | None,
) -> tuple[str, list[str]]:
    """Create a Device row + assign the primary IP.

    Returns ``(device_uuid, warnings)``. Everything after the device POST is
    best-effort — the device exists either way — so those failures come back
    as warnings for the caller to surface rather than being logged and
    forgotten. They used to be log-only, which meant a device created with no
    primary IP (the usual cause: no parent prefix in namespace ``Global``)
    was reported to the operator as an unqualified success.

    Raises HTTPException on a 4xx/5xx from the device POST itself so the
    caller can surface a meaningful row-level error in bulk mode.
    """
    body: dict[str, Any] = {
        "name": name,
        "location": site_id,
        "device_type": device_type_id,
        "role": device_role_id,
        "status": "Active",
        "custom_fields": {"daalu_transport": transport},
    }
    if platform_id:
        body["platform"] = platform_id
    if nautobot_tenant_id:
        body["tenant"] = nautobot_tenant_id

    resp = await client.post("/api/dcim/devices/", json=body)
    if resp.status_code not in (200, 201):
        raise HTTPException(
            502,
            f"Nautobot rejected device create: HTTP {resp.status_code} — {resp.text[:200]}",
        )
    device_uuid = str(resp.json()["id"])
    warnings: list[str] = []

    # Attach the primary IP — Nautobot requires a two-step flow:
    # create the IP, attach it to an interface, then PATCH the device.
    # Nautobot 2.x: an IP needs a namespace (or parent prefix), it can only
    # be device-assigned through the ip-address-to-interface m2m, and
    # primary_ip4 refuses any IP not assigned to one of the device's
    # interfaces — the 1.x assigned_object_* shortcut is gone. Verified
    # against the NV-CM-bundled Nautobot 2.4.
    ip_resp = await client.post(
        "/api/ipam/ip-addresses/",
        json={
            "address": primary_ip,
            "status": "Active",
            "namespace": {"name": "Global"},
        },
    )
    if ip_resp.status_code in (200, 201):
        ip_uuid = ip_resp.json()["id"]
        iface_resp = await client.post(
            "/api/dcim/interfaces/",
            json={
                "device": device_uuid,
                "name": "mgmt0",
                "type": "virtual",
                "status": "Active",
                "description": "management (daalu-created)",
            },
        )
        if iface_resp.status_code in (200, 201):
            assign_resp = await client.post(
                "/api/ipam/ip-address-to-interface/",
                json={
                    "ip_address": ip_uuid,
                    "interface": iface_resp.json()["id"],
                },
            )
            if assign_resp.status_code not in (200, 201):
                logger.warning(
                    "sot_devices.create.ip_iface_assign_failed",
                    extra={
                        "device_id": device_uuid,
                        "status": assign_resp.status_code,
                        "body": assign_resp.text[:200],
                    },
                )
                warnings.append(
                    f"could not attach {primary_ip} to mgmt0: "
                    f"HTTP {assign_resp.status_code} — {assign_resp.text[:120]}"
                )
        else:
            logger.warning(
                "sot_devices.create.interface_failed",
                extra={
                    "device_id": device_uuid,
                    "status": iface_resp.status_code,
                    "body": iface_resp.text[:200],
                },
            )
            warnings.append(
                f"could not create the mgmt0 interface: "
                f"HTTP {iface_resp.status_code} — {iface_resp.text[:120]}"
            )
        patch_resp = await client.patch(
            f"/api/dcim/devices/{device_uuid}/",
            json={"primary_ip4": ip_uuid},
        )
        if patch_resp.status_code not in (200, 201):
            logger.warning(
                "sot_devices.create.primary_ip_assign_failed",
                extra={"device_id": device_uuid, "ip_uuid": ip_uuid},
            )
            warnings.append(
                f"device has no primary IP: setting {primary_ip} returned "
                f"HTTP {patch_resp.status_code} — {patch_resp.text[:120]}"
            )
    else:
        logger.warning(
            "sot_devices.create.ip_failed",
            extra={
                "device_id": device_uuid,
                "status": ip_resp.status_code,
                "body": ip_resp.text[:200],
            },
        )
        warnings.append(
            f"device has no primary IP: creating {primary_ip} returned "
            f"HTTP {ip_resp.status_code} — {ip_resp.text[:120]}"
            + (
                " (no parent prefix in namespace Global?)"
                if ip_resp.status_code == 400
                else ""
            )
        )
    return device_uuid, warnings


@router.post("", response_model=DeviceOut)
async def create_device(
    payload: DeviceCreateIn,
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Create a new Device row in Nautobot, pre-stamped with daalu_transport.

    Admin-only. The Nautobot Tenant the device gets associated with is
    derived from the daalu tenant slug — looked up via the integration
    row's ``nautobot_tenant_id`` (set by hosted provisioning) or
    inferred via the daalu tenant slug for BYO setups.
    """
    tenant_id = user.tenant_id
    nautobot_tenant_id = await _resolve_nautobot_tenant_id(db, tenant_id)

    async with await _nautobot_client(db, tenant_id, env) as client:
        await _ensure_daalu_transport_field(client)
        device_uuid, warnings = await _create_one_device(
            client,
            name=payload.name,
            primary_ip=payload.primary_ip,
            site_id=payload.site_id,
            device_type_id=payload.device_type_id,
            device_role_id=payload.device_role_id,
            platform_id=payload.platform_id,
            transport=payload.transport,
            nautobot_tenant_id=nautobot_tenant_id,
        )
        if warnings:
            logger.warning(
                "sot_devices.create.partial",
                extra={"device_id": device_uuid, "warnings": warnings},
            )

    # Re-fetch via the SoT layer so the response shape matches list/get
    sot = NautobotSoT(env=env)
    dev = await sot.get_device(db, tenant_id, device_uuid)
    if dev is None:
        # Should be unreachable — Nautobot just confirmed the create
        raise HTTPException(502, "device created but not visible to get_device")
    return dev


# ── Bulk import ─────────────────────────────────────────────────────────


class BulkRowResult(BaseModel):
    row: int
    name: str
    primary_ip: str
    transport: str
    site: str
    device_type: str
    role: str
    platform: str | None = None
    status: str  # 'valid' | 'error' | 'created'
    error: str | None = None
    device_id: str | None = None
    # Device created, but something after it didn't land (usually the primary
    # IP). The row is still a success — it just isn't a clean one, and saying
    # so beats reporting an unqualified success for a device with no IP.
    warnings: list[str] = []


class CatalogPlanOut(BaseModel):
    """Catalog objects the upload names that the tenant's Nautobot lacks.

    Populated on both phases: on dry-run it is what *will* be created, which is
    the operator's chance to catch a typo before it becomes a real role; on
    apply it is what was asked for, with ``created_catalog`` saying what landed.
    """

    sites: list[str] = []
    device_types: list[str] = []
    roles: list[str] = []
    platforms: list[str] = []
    # Parent prefixes for the rows' primary IPs. Nautobot 2.x refuses an IP
    # with no covering prefix, which is what leaves devices with no primary IP.
    prefixes: list[str] = []


class BulkImportResult(BaseModel):
    dry_run: bool
    summary: dict[str, int]
    rows: list[BulkRowResult]
    # Problems that apply to the run as a whole rather than to any one row —
    # an unreadable catalog, a custom field that couldn't be created.
    errors: list[str] = []
    # Only set when the caller asked us to fill the catalog in
    # (``create_missing``); null means the import resolved against what
    # Nautobot already had.
    planned_catalog: CatalogPlanOut | None = None
    created_catalog: list[str] = []


async def _ensure_daalu_transport_field(client: httpx.AsyncClient) -> str | None:
    """Make sure Nautobot has the ``daalu_transport`` custom field on Device.

    Every device Daalu creates is stamped with it, and the driver dispatch
    reads it back (``core/sot/nautobot.py``) — so it is a Daalu invariant, not
    something an operator should have to know about. A stock NV-CM-bundled
    Nautobot doesn't have it, and Nautobot rejects the whole device POST with
    a 400 when a payload names a custom field that doesn't exist, so without
    this every import fails at apply with an error that points nowhere near
    the cause.

    Best-effort by design: returns a message on failure instead of raising, so
    a Nautobot that forbids schema writes degrades to the (clear) device-POST
    error rather than blocking the import outright.
    """
    try:
        # List and match in Python rather than filtering server-side: the
        # bundled Nautobot 400s on `?key=`, and a 400 read as "not found" would
        # make this POST — and report the resulting "already exists" as a
        # failure — on every import after the first. The list is a handful of
        # rows, so this costs nothing.
        resp = await client.get("/api/extras/custom-fields/", params={"limit": 250})
        if resp.status_code == 200:
            fields = (resp.json() or {}).get("results") or []
            if any(f.get("key") == "daalu_transport" for f in fields):
                return None
        create = await client.post(
            "/api/extras/custom-fields/",
            json={
                "key": "daalu_transport",
                "label": "Daalu Transport",
                "type": "text",
                "content_types": ["dcim.device"],
                "description": "Driver transport daalu dispatches with (created by daalu).",
            },
        )
        if create.status_code not in (200, 201):
            # A concurrent importer may have won the race; that's success.
            if "already exists" in create.text:
                return None
            return (
                "could not create the daalu_transport custom field in Nautobot: "
                f"HTTP {create.status_code} — {create.text[:120]}"
            )
    except httpx.HTTPError as e:
        return f"could not reach Nautobot to check the daalu_transport custom field: {e}"
    logger.info("sot_devices.custom_field.created", extra={"key": "daalu_transport"})
    return None


def _catalog_index(items: list[CatalogItem]) -> dict[str, str]:
    """Lower-case name → UUID, plus slug and slugified-name aliases.

    Operators write catalog references the way they appear in a device file
    (``cumulus-linux``), not the way Nautobot displays them (``Cumulus
    Linux``). Nautobot 1.x made that work because every object carried a
    ``slug``; 2.x dropped ``slug`` from Platform and friends, so a
    name-and-slug index silently stopped matching the hyphenated form and
    every row failed with "unknown platform" against a catalog that plainly
    contained it. Indexing a slugified name too closes that gap for good.

    Exact names and real slugs are indexed first and win: the slugified
    aliases only fill keys nothing else claimed, so an object literally named
    ``cumulus-linux`` still beats one named ``Cumulus Linux``.
    """
    idx: dict[str, str] = {}
    for it in items:
        for exact in ((it.name or "").strip().lower(), (it.slug or "").strip().lower()):
            # Guard the empty key: a blank/whitespace name would otherwise
            # become a catch-all that resolves every unmatched reference.
            if exact:
                idx[exact] = it.id
    for it in items:
        for alias in (_slugify(it.name or ""), _slugify(it.slug or "")):
            if alias:
                idx.setdefault(alias, it.id)
    return idx


def _resolve_row(
    row: ParsedRow,
    *,
    sites: dict[str, str],
    device_types: dict[str, str],
    roles: dict[str, str],
    platforms: dict[str, str],
    plan: CatalogPlan | None = None,
) -> tuple[BulkRowResult, dict[str, str] | None]:
    """Resolve catalog names → IDs for one row.

    Returns the row-result plus, when valid, a dict with the resolved IDs
    ready for :func:`_create_one_device`. ``None`` for the second value
    means the row has errors and should be skipped on apply.

    ``plan`` is the set of catalog objects the run is going to create first
    (dry-run only): a name that is missing but planned isn't an error, it's a
    note. Without that, a preview of an import into a fresh environment shows
    nothing but red and the operator has no way to tell "your file is wrong"
    apart from "your catalog is empty".
    """
    base = BulkRowResult(
        row=row.row_index,
        name=row.name,
        primary_ip=row.primary_ip,
        transport=row.transport,
        site=row.site,
        device_type=row.device_type,
        role=row.role,
        platform=row.platform,
        status="valid",
    )
    if row.parse_error:
        return base.model_copy(update={"status": "error", "error": row.parse_error}), None

    errors: list[str] = []
    pending: list[str] = []

    def _lookup(index: dict[str, str], kind: str, label: str, value: str) -> str | None:
        found = index.get(value.lower())
        if found:
            return found
        if plan is not None and plan.has(kind, value):
            pending.append(f"will create {label} '{value}'")
            return None
        errors.append(f"unknown {label} '{value}'")
        return None

    site_id = _lookup(sites, "sites", "site", row.site)
    type_id = _lookup(device_types, "device_types", "device_type", row.device_type)
    role_id = _lookup(roles, "roles", "role", row.role)
    platform_id: str | None = None
    if row.platform:
        platform_id = _lookup(platforms, "platforms", "platform", row.platform)

    if errors:
        return base.model_copy(
            update={"status": "error", "error": "; ".join(errors)}
        ), None

    if pending:
        # Valid, but only once the plan has run — so no IDs to hand back. The
        # apply phase re-resolves against the catalog it just filled in.
        return base.model_copy(update={"warnings": pending}), None

    return base, {
        "site_id": site_id or "",
        "device_type_id": type_id or "",
        "device_role_id": role_id or "",
        "platform_id": platform_id or "",
    }


@router.post("/bulk-import", response_model=BulkImportResult)
async def bulk_import_devices(
    file: UploadFile = File(...),
    dry_run: bool = Query(default=True),
    create_missing: bool = Query(
        default=False,
        description=(
            "Create the sites, device types, roles, platforms and parent "
            "prefixes the file names but the environment's Nautobot doesn't "
            "have yet. On dry-run they are only reported (as planned_catalog "
            "and per-row warnings); nothing is written until apply."
        ),
    ),
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Bulk-create devices from a YAML or Excel upload.

    Two-phase by default: ``dry_run=true`` (the default) parses + resolves
    names against the tenant's Nautobot catalog and returns the per-row
    outcome without writing. The frontend shows the preview; the operator
    re-submits with ``dry_run=false`` to actually create the devices.

    Per-row errors don't abort the batch — each row succeeds or fails
    independently, mirroring what the operator sees in the dry-run table.

    ``create_missing`` fills the catalog in as part of the run. A newly
    provisioned NV-CM environment has no locations at all and only NVIDIA's
    reference device types, so without it the first import into every new
    environment fails every row on names that simply don't exist yet — the
    recurring failure this flag exists to end. The dry-run still shows exactly
    which objects would be created before anything is written.
    """
    tenant_id = user.tenant_id
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "upload is empty")

    try:
        rows = parse_upload(file.filename or "", file.content_type, raw)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if not rows:
        raise HTTPException(400, "no device rows found in upload")

    catalog_resp = await catalog(env=env, db=db, tenant_id=tenant_id)
    sites = _catalog_index(catalog_resp.sites)
    device_types = _catalog_index(catalog_resp.device_types)
    roles = _catalog_index(catalog_resp.device_roles)
    platforms = _catalog_index(catalog_resp.platforms)

    nautobot_tenant_id = await _resolve_nautobot_tenant_id(db, tenant_id)

    run_errors = list(catalog_resp.errors)
    plan: CatalogPlan | None = None
    if create_missing:
        # Prefixes aren't part of the wizard catalog (nothing renders them), so
        # they're read here — an IP with no covering prefix is what silently
        # costs a device its primary IP.
        held: set[str] = set()
        if not catalog_resp.errors:
            try:
                async with await _nautobot_client(db, tenant_id, env) as client:
                    held = await existing_prefixes(client)
            except HTTPException:
                raise
            except Exception as e:  # noqa: BLE001 — planning must not 500
                run_errors.append(f"could not read the prefix list: {e}")
        plan = plan_catalog_additions(
            rows,
            sites=sites,
            device_types=device_types,
            roles=roles,
            platforms=platforms,
            prefixes=held,
        )

    def _resolve_all(
        active_plan: CatalogPlan | None,
    ) -> list[tuple[BulkRowResult, dict[str, str] | None, ParsedRow]]:
        return [
            (*_resolve_row(
                row,
                sites=sites,
                device_types=device_types,
                roles=roles,
                platforms=platforms,
                plan=active_plan,
            ), row)
            for row in rows
        ]

    results: list[BulkRowResult] = []
    resolved = _resolve_all(plan)
    created_catalog: list[str] = []

    if dry_run:
        results = [r for r, _, _ in resolved]
    else:
        async with await _nautobot_client(db, tenant_id, env) as client:
            if plan is not None and not plan.is_empty:
                ensured = await ensure_catalog(client, plan)
                created_catalog = ensured.created
                run_errors.extend(ensured.errors)
                if ensured.created:
                    # Re-read rather than trusting the ids we just POSTed: a
                    # partial run still resolves whatever did land, and the
                    # names come back in Nautobot's own casing.
                    catalog_resp = await catalog(env=env, db=db, tenant_id=tenant_id)
                    sites = _catalog_index(catalog_resp.sites)
                    device_types = _catalog_index(catalog_resp.device_types)
                    roles = _catalog_index(catalog_resp.device_roles)
                    platforms = _catalog_index(catalog_resp.platforms)
                # Anything still unresolved is a genuine error now — the plan
                # has had its turn, so resolve without it.
                resolved = _resolve_all(None)
            # Before the first POST: Nautobot 400s the whole device create if
            # the payload names a custom field the instance doesn't define.
            if any(ids is not None for _, ids, _ in resolved):
                field_err = await _ensure_daalu_transport_field(client)
                if field_err:
                    run_errors.append(field_err)
            for result, ids, row in resolved:
                if ids is None:
                    results.append(result)
                    continue
                try:
                    device_uuid, row_warnings = await _create_one_device(
                        client,
                        name=row.name,
                        primary_ip=row.primary_ip,
                        site_id=ids["site_id"],
                        device_type_id=ids["device_type_id"],
                        device_role_id=ids["device_role_id"],
                        platform_id=ids["platform_id"] or None,
                        transport=row.transport,
                        nautobot_tenant_id=nautobot_tenant_id,
                    )
                    results.append(
                        result.model_copy(
                            update={
                                "status": "created",
                                "device_id": device_uuid,
                                "warnings": row_warnings,
                            }
                        )
                    )
                except HTTPException as e:
                    results.append(
                        result.model_copy(
                            update={"status": "error", "error": str(e.detail)}
                        )
                    )
                except Exception as e:  # noqa: BLE001 — per-row boundary
                    logger.exception(
                        "sot_devices.bulk_import.row_failed",
                        extra={"row": row.row_index, "name": row.name},
                    )
                    results.append(
                        result.model_copy(
                            update={
                                "status": "error",
                                "error": f"{type(e).__name__}: {e}",
                            }
                        )
                    )

    summary = {
        "total": len(results),
        "valid": sum(1 for r in results if r.status == "valid"),
        "errors": sum(1 for r in results if r.status == "error"),
        "created": sum(1 for r in results if r.status == "created"),
        "warnings": sum(1 for r in results if r.warnings),
    }
    return BulkImportResult(
        dry_run=dry_run,
        summary=summary,
        rows=results,
        errors=run_errors,
        planned_catalog=(
            CatalogPlanOut(
                sites=plan.sites,
                device_types=plan.device_types,
                roles=plan.roles,
                platforms=plan.platforms,
                prefixes=plan.prefixes,
            )
            if plan is not None
            else None
        ),
        created_catalog=created_catalog,
    )


# ── On-demand reconciliation ────────────────────────────────────────────


class ReconcileResultOut(BaseModel):
    device_id: str
    status: str  # 'in_sync' | 'drift' | 'skipped' | 'error'
    detail: str | None = None
    proposal_id: str | None = None


@router.post("/{device_id}/reconcile", response_model=ReconcileResultOut)
async def reconcile_device(
    device_id: str,
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Run the reconciler synchronously for a single device.

    Admin-only. Wraps the same per-device path used by the Celery beat
    sweep (``workers/reconciler.py``) — extracted so the "Reconcile now"
    button can give the operator an immediate result instead of waiting
    for the next scheduled sweep.
    """
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.device import get_device_adapter
    from daalu_automation.core.events import EventEnvelope, publish
    from daalu_automation.core.sot.models import Actor
    from daalu_automation.models import (
        ChangeProposal,
        ChangeProposalKind,
        ChangeProposalStatus,
    )

    tenant_id = user.tenant_id
    sot = NautobotSoT(env=env)
    try:
        device = await sot.get_device(db, tenant_id, device_id)
    except NautobotUnavailable as e:
        raise HTTPException(409, str(e)) from e
    if device is None:
        raise HTTPException(404, f"device {device_id} not found")

    # Don't fire a fresh proposal if one is already open — same rule the
    # background sweep follows. We still tell the user what happened.
    open_row = (
        await db.execute(
            select(ChangeProposal.id)
            .where(
                ChangeProposal.tenant_id == tenant_id,
                ChangeProposal.device_id == device_id,
                ChangeProposal.status.in_(
                    (ChangeProposalStatus.pending, ChangeProposalStatus.approved)
                ),
            )
            .limit(1)
        )
    ).first()
    if open_row is not None:
        return ReconcileResultOut(
            device_id=device_id,
            status="skipped",
            detail="an open proposal already exists for this device",
            proposal_id=str(open_row[0]),
        )

    intended = await sot.get_intended_config(db, tenant_id, device_id)
    if intended is None:
        return ReconcileResultOut(
            device_id=device_id,
            status="skipped",
            detail="device has no daalu_intent set — author it from the device page first",
        )

    adapter = get_device_adapter(device.transport)
    try:
        creds = await cps.resolve_credentials(db, tenant_id, device)
        observed = await adapter.collect(creds, intended_hint=intended.facts)
        diff = await adapter.diff(observed, intended.facts)
    except Exception as e:  # noqa: BLE001 — surfaces device-level failures
        logger.exception(
            "sot_devices.reconcile.device_failed",
            extra={"device_id": device_id, "error": f"{type(e).__name__}: {e}"},
        )
        return ReconcileResultOut(
            device_id=device_id,
            status="error",
            detail=f"{type(e).__name__}: {e}",
        )

    if not diff.has_changes:
        await publish(
            EventEnvelope(
                tenant_id=str(tenant_id),
                type="device.observed.snapshot",
                module="sot",
                source="reconciler",
                severity="info",
                summary=f"device {device.name} in sync",
                payload={"device_id": device_id, "facts_changed": []},
            )
        )
        return ReconcileResultOut(
            device_id=device_id,
            status="in_sync",
            detail="no drift detected",
        )

    rendered_intended = await adapter.render(intended.facts)
    rendered_observed = await adapter.render(observed)
    proposal = await cps.propose(
        db,
        tenant_id,
        device_id=device_id,
        # Same environment the drift was observed in, so the executor
        # re-resolves this device in the Nautobot it actually came from.
        environment_slug=env,
        kind=ChangeProposalKind.drift,
        intended_config=cps.serialize_rendered_files(rendered_intended.files),
        observed_config=cps.serialize_rendered_files(rendered_observed.files),
        diff=diff.unified_diff,
        renderer_version=rendered_intended.renderer_version,
        evidence={
            "triggered_by": "operator.reconcile_now",
            "confidence": 1.0,
            "facts_changed": diff.facts_changed,
        },
        actor=Actor(kind="user", name=user.email or "operator"),
    )
    return ReconcileResultOut(
        device_id=device_id,
        status="drift",
        detail="drift detected — opened a change proposal",
        proposal_id=str(proposal.id),
    )


@router.put("/{device_id}/intent", response_model=IntentOut)
async def update_intent(
    device_id: str,
    payload: IntentUpdateIn,
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Write ``daalu_intent`` for the device.

    The frontend's intent editor (form-based for the common LinuxFacts /
    RedfishFacts / NetworkFacts shapes) posts the resulting JSON blob
    here. Validation happens server-side via the right facts pydantic
    model — invalid shapes return 422 instead of corrupting the SoT.
    """
    tenant_id = user.tenant_id
    sot = NautobotSoT(env=env)

    # Discover transport so we know which facts schema to validate against
    try:
        dev = await sot.get_device(db, tenant_id, device_id)
    except NautobotUnavailable as e:
        raise HTTPException(409, str(e)) from e
    if dev is None:
        raise HTTPException(404, f"device {device_id} not found")

    try:
        facts = _parse_intent(dev.transport, payload.facts)
    except Exception as e:  # noqa: BLE001 — pydantic ValidationError
        raise HTTPException(422, f"intent payload did not validate: {e}") from e

    intended = IntendedConfig(
        device_id=device_id,
        revision="(unset)",  # SoT will recompute on write
        facts=facts,
        extra={},
        fetched_at=datetime.now(tz=timezone.utc),
    )
    try:
        rev = await sot.put_intended_config(db, tenant_id, device_id, intended)
    except NautobotUnavailable as e:
        raise HTTPException(502, f"Nautobot rejected the write: {e}") from e

    return IntentOut(
        device_id=device_id,
        revision=rev.revision,
        transport=dev.transport,
        facts=_facts_to_blob(facts),
        fetched_at=intended.fetched_at,
    )


@router.delete("/{device_id}", status_code=204)
async def delete_device(
    device_id: str,
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Remove the device from Nautobot.

    Admin-only. Daalu's own row references the device by Nautobot UUID
    but doesn't FK to it; dangling references in old ChangeProposal
    rows are fine — the executor will skip them with
    ``executor.device_not_found`` on the next tick.
    """
    tenant_id = user.tenant_id
    async with await _nautobot_client(db, tenant_id, env) as client:
        resp = await client.delete(f"/api/dcim/devices/{device_id}/")
        if resp.status_code not in (200, 202, 204):
            raise HTTPException(
                502,
                f"Nautobot rejected device delete: HTTP {resp.status_code} — {resp.text[:200]}",
            )
    return None


# ── Device telemetry (logs + metrics) onboarding ─────────────────────────
#
# "Connect monitoring" for the fabric: best-effort push of the device-side
# config (rsyslog forward → the tenant's syslog collector; node_exporter if
# present) over the tunnel-aware SSH path, plus a status probe that checks
# the tenant's own Prometheus/Loki for the resulting series/streams. The
# cluster-side collectors (Alloy syslog receiver + the network-devices
# ScrapeConfig) ship as manifests — see examples/telemetry/ — and
# will fold into the controller in a follow-up.


class MonitoringStatusOut(BaseModel):
    devices_total: int
    metrics_devices: list[str]
    logs_devices: list[str]
    prometheus_ok: bool
    loki_ok: bool
    detail: str = ""


class MonitoringConnectIn(BaseModel):
    collector_host: str = Field(min_length=1, max_length=253)
    collector_port: int = Field(default=5514, ge=1, le=65535)


class MonitoringDeviceResult(BaseModel):
    device: str
    ok: bool
    detail: str = ""


class MonitoringConnectOut(BaseModel):
    results: list[MonitoringDeviceResult]


@router.get("/monitoring/status", response_model=MonitoringStatusOut)
async def monitoring_status(
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    """Which fabric devices are reporting metrics / shipping logs.

    Reads the tenant's OWN observability stores (the prometheus/loki
    integrations, tunnel-routed) — the same stores the copilot's
    query_prometheus / query_loki tools use.
    """
    from daalu_automation.core.observability_console import (
        ObsConsoleError,
        _loki_get,
        _resolve,
    )

    sot = NautobotSoT(env=env)
    try:
        devices = await sot.list_devices(db, tenant_id)
    except NautobotUnavailable:
        devices = []

    metrics_devices: list[str] = []
    logs_devices: list[str] = []
    prom_ok = loki_ok = False
    detail = ""

    try:
        base, auth, proxy = await _resolve("prometheus", tenant_id)
        data = await _loki_get(
            base, auth, proxy, "/api/v1/query",
            {"query": 'up{job="network-devices"} == 1'},
        )
        metrics_devices = sorted(
            r["metric"].get("device", "")
            for r in data.get("data", {}).get("result", [])
            if r["metric"].get("device")
        )
        prom_ok = True
    except (ObsConsoleError, Exception) as e:  # noqa: BLE001 — status is best-effort
        detail = f"prometheus: {str(e)[:120]}"

    try:
        base, auth, proxy = await _resolve("loki", tenant_id)
        data = await _loki_get(
            base, auth, proxy, "/loki/api/v1/label/device/values",
            {"since": "1h"},
        )
        logs_devices = sorted(data.get("data") or [])
        loki_ok = True
    except (ObsConsoleError, Exception) as e:  # noqa: BLE001
        detail = (detail + " · " if detail else "") + f"loki: {str(e)[:120]}"

    return MonitoringStatusOut(
        devices_total=len(devices),
        metrics_devices=metrics_devices,
        logs_devices=logs_devices,
        prometheus_ok=prom_ok,
        loki_ok=loki_ok,
        detail=detail,
    )


@router.post("/monitoring/connect", response_model=MonitoringConnectOut)
async def monitoring_connect(
    payload: MonitoringConnectIn,
    env: str | None = ENV_QUERY,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Push telemetry config to every SSH-able fabric device (best-effort).

    Per device: write an rsyslog forward rule aimed at the tenant's syslog
    collector (RFC5424/TCP) and restart rsyslog; enable node_exporter when
    the device has one. Runs over the same tunnel-aware SSH the LLDP probe
    uses. Devices where a step fails report the error instead of failing
    the whole call — real fabrics are heterogeneous.
    """
    import re as _re
    import shlex

    if not _re.match(r"^[A-Za-z0-9._-]+$", payload.collector_host):
        raise HTTPException(400, "collector_host must be a bare hostname/IP")

    from daalu_automation.core.change_proposals import resolve_credentials
    from daalu_automation.core.device.linux_ssh import _open

    sot = NautobotSoT(env=env)
    try:
        devices = await sot.list_devices(db, user.tenant_id)
    except NautobotUnavailable as e:
        raise HTTPException(409, str(e)) from e
    targets = [d for d in devices if d.transport == "linux_ssh" and d.primary_ip]

    conf_line = (
        f"*.* @@{payload.collector_host}:{payload.collector_port}"
        ";RSYSLOG_SyslogProtocol23Format"
    )
    sem = asyncio.Semaphore(8)

    async def one(dev) -> MonitoringDeviceResult:
        try:
            creds = await resolve_credentials(db, user.tenant_id, dev)
        except Exception as e:  # noqa: BLE001
            return MonitoringDeviceResult(
                device=dev.name, ok=False, detail=f"no credentials: {str(e)[:120]}"
            )
        sudo = "sudo -n " if creds.sudo else ""
        cmds = (
            f"printf '%s\\n' {shlex.quote(conf_line)} | "
            f"{sudo}tee /etc/rsyslog.d/99-daalu-telemetry.conf >/dev/null"
            f" && ({sudo}systemctl restart rsyslog 2>/dev/null"
            f" || {sudo}pkill -HUP rsyslogd 2>/dev/null || true)"
            f" && (command -v node_exporter >/dev/null &&"
            f" ({sudo}systemctl enable --now prometheus-node-exporter"
            f" 2>/dev/null || true) || true)"
        )
        async def _push() -> MonitoringDeviceResult:
            async with _open(creds) as conn:
                r = await conn.run(cmds, check=False)
                if r.exit_status == 0:
                    return MonitoringDeviceResult(device=dev.name, ok=True)
                return MonitoringDeviceResult(
                    device=dev.name,
                    ok=False,
                    detail=(r.stderr or r.stdout or "").strip()[:160],
                )

        async with sem:
            try:
                return await asyncio.wait_for(_push(), timeout=20)
            except Exception as e:  # noqa: BLE001
                return MonitoringDeviceResult(
                    device=dev.name, ok=False, detail=str(e)[:160]
                )

    results = list(await asyncio.gather(*(one(d) for d in targets)))
    return MonitoringConnectOut(results=results)
