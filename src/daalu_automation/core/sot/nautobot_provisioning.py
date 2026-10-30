"""Hosted-Nautobot provisioning.

Tenants who opt into our managed Nautobot get a fully scoped slice
provisioned automatically at onboarding time:

1. **Nautobot Tenant** matching the daalu tenant slug — every device,
   IP, prefix, circuit etc. their account writes is stamped with this.
2. **ObjectPermission** scoped via a constraint dict to that tenant's
   slug, attached to the platform service user. Constraint mirrors
   Network-to-Code's documented per-tenant SaaS pattern.
3. **APIToken** minted for the service user. The token only has the
   ObjectPermission's reach, so the customer can read/write their own
   tenant's data and nothing else even though they share a Postgres
   with every other customer.

The single Nautobot instance keeps operational complexity down — see
:doc:`[[sot-nautobot-only]]`. Hard isolation comes from the
ObjectPermission constraints; **a bug in those constraints is a
cross-tenant data leak**, so the integration tests pin the constraint
shape exactly.

BYO tenants don't touch this module at all — they fill in url+token
on the onboarding step directly. :func:`is_hosted_enabled` is what
the API surface checks to decide whether to even offer hosted mode.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import httpx

from daalu_automation.config import get_settings

logger = logging.getLogger(__name__)


# Permissions on the granted ObjectPermission. We grant "view" + "add"
# + "change" + "delete" on every DCIM and IPAM model — the customer
# owns their tenant's data and should be able to manage everything
# inside it. We do NOT grant Tenant-model perms (so the customer can't
# rename or delete their own tenant; that's a daalu-platform operation).
# Adding new SoT-facing object models in the future will require an
# explicit grant here — fail-closed by default.
GRANTED_ACTIONS: tuple[str, ...] = ("view", "add", "change", "delete")
GRANTED_OBJECT_TYPES: tuple[str, ...] = (
    # DCIM — devices, interfaces, racks, cables, etc.
    "dcim.device",
    "dcim.interface",
    "dcim.devicetype",
    "dcim.devicerole",
    "dcim.platform",
    "dcim.location",
    "dcim.rack",
    "dcim.cable",
    # IPAM — IP addresses, prefixes, VLANs
    "ipam.ipaddress",
    "ipam.prefix",
    "ipam.vlan",
    "ipam.vlangroup",
    # Extras — config contexts (the daalu_intent / daalu_observed bags)
    "extras.configcontext",
    "extras.tag",
    "extras.note",
)


class ProvisioningError(RuntimeError):
    """Provisioning failed and the partial state, if any, was rolled back."""


def _raise_clean(resp: httpx.Response, *, what: str) -> None:
    """Translate an HTTP error response into a ProvisioningError.

    The route layer maps ProvisioningError → 502 with the detail in
    the response body. Without this wrapper, ``raise_for_status()``
    bubbles ``httpx.HTTPStatusError`` past the route handler and the
    user sees a generic 500 with no actionable message.
    """
    if resp.status_code < 400:
        return
    raise ProvisioningError(
        f"{what}: HTTP {resp.status_code} from Nautobot — {resp.text[:300]}"
    )


@dataclass
class ProvisionedNautobot:
    """Output of a successful :func:`provision_tenant` call."""

    url: str            # full URL the customer's integration row will use
    token: str          # cleartext APIToken — store encrypted on receipt
    tenant_slug: str    # the Nautobot tenant slug we created
    tenant_id: str      # Nautobot's UUID for the tenant
    permission_id: str  # the ObjectPermission's UUID — for later teardown


def is_hosted_enabled() -> bool:
    """Whether the managed-Nautobot provisioning path is available.

    Two ways the deploy can offer hosted mode:

    1. The new per-tenant controller is wired
       (``nautobot_controller_url`` set). This is the post-2026-05
       path — every tenant gets their own isolated Nautobot stack.
    2. The legacy shared-Nautobot path
       (``managed_nautobot_url`` + admin token). Still supported for
       deployments that haven't migrated; the per-tenant controller
       is recommended for new installs.

    The route uses :func:`provision_via_controller` when (1) is
    available and falls back to :func:`provision_tenant` (the legacy
    shared-Nautobot path) when only (2) is.
    """
    s = get_settings()
    return is_controller_enabled() or bool(s.managed_nautobot_url) and bool(s.managed_nautobot_admin_token)


def is_controller_enabled() -> bool:
    """Whether the per-tenant nautobot-controller is reachable.

    The recommended hosted-Nautobot path post-2026-05. When this is
    on, the provision route routes to the controller and the
    customer gets a fully isolated Nautobot stack (their own
    Postgres, their own admin user, their own URL). See engineer
    chapter 60.
    """
    s = get_settings()
    return bool(s.nautobot_controller_url)


async def provision_via_controller(
    *,
    tenant_id: uuid.UUID,
    target_cluster_tunnel_id: uuid.UUID | None = None,
) -> ProvisionedNautobot:
    """Provision via the per-tenant nautobot-controller.

    Posts to the controller's REST endpoint, polls for ``state ==
    'active'``, returns the URL + admin token the caller writes into
    the Integration row.

    ``target_cluster_tunnel_id`` switches to customer-cluster mode
    (Phase 2) — the controller will deploy Nautobot inside the
    customer's cluster reached via WireGuard. None → operator-cluster
    mode.

    Provisioning takes 60-180 s on a cold tenant (image pulls +
    Django migrations on first boot). The route is meant to be
    awaited from the wizard's "Provision" button click; the wizard
    shows a spinner.
    """
    s = get_settings()
    if not s.nautobot_controller_url:
        raise ProvisioningError(
            "nautobot_controller_url is not configured on this deploy"
        )
    from daalu_automation.core.service_tokens import mint_service_token

    token = mint_service_token(
        tenant_id=str(tenant_id),
        user_id=str(tenant_id),  # controller checks tenant_id only
        purpose="nautobot-provision",
        ttl_seconds=300,  # provisioning round-trip can take a while
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    base = s.nautobot_controller_url.rstrip("/")
    body = {
        "target_cluster_tunnel_id": (
            str(target_cluster_tunnel_id) if target_cluster_tunnel_id else None
        ),
    }
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        resp = await client.post(f"{base}/tenants/{tenant_id}", json=body)
        if resp.status_code not in (200, 201):
            raise ProvisioningError(
                f"nautobot-controller rejected upsert: "
                f"HTTP {resp.status_code} — {resp.text[:300]}"
            )
        view = resp.json()
        # Poll until active. Cap at ~3 minutes total — beyond that the
        # operator should look at the controller logs themselves.
        import asyncio
        deadline = 180
        waited = 0
        while view.get("state") != "active":
            if view.get("state") == "error":
                raise ProvisioningError(
                    f"nautobot-controller marked tenant errored: "
                    f"{view.get('last_error') or '(no detail)'}"
                )
            if waited >= deadline:
                raise ProvisioningError(
                    f"nautobot-controller did not reach 'active' in "
                    f"{deadline}s (currently '{view.get('state')}'); the pod "
                    "is likely still booting — retry in a minute"
                )
            await asyncio.sleep(5)
            waited += 5
            r = await client.get(f"{base}/tenants/{tenant_id}")
            if r.status_code != 200:
                raise ProvisioningError(
                    f"nautobot-controller poll failed: HTTP {r.status_code}"
                )
            view = r.json()
    return ProvisionedNautobot(
        url=view["url"],
        token=view["admin_token"],
        tenant_slug=str(tenant_id),  # the new model doesn't need a slug
        tenant_id=view["id"],
        permission_id="",  # n/a in per-tenant mode; field kept for API compat
    )


def _build_admin_client(url: str, token: str) -> httpx.AsyncClient:
    """Construct the AsyncClient used to talk to the managed Nautobot as admin.

    Factored to module level so tests can monkeypatch with
    ``httpx.MockTransport`` — same pattern as
    :mod:`core.sot.nautobot._build_http_client`.
    """
    return httpx.AsyncClient(
        base_url=url.rstrip("/"),
        timeout=30.0,
        headers={
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )


async def _find_user_id(client: httpx.AsyncClient, username: str) -> str:
    """Resolve the Nautobot user UUID for the configured service user.

    Nautobot's user API is at /api/users/users/. We expect the platform
    service user to already exist — provisioning fails loudly if not,
    because creating it requires a one-time operator setup decision
    (which permissions to bootstrap with, what scope of admin rights).
    """
    resp = await client.get("/api/users/users/", params={"username": username})
    _raise_clean(resp, what=f"user lookup for {username!r}")
    results = (resp.json() or {}).get("results") or []
    if not results:
        raise ProvisioningError(
            f"managed Nautobot has no user named {username!r}; "
            "create the service user before tenants can self-onboard"
        )
    return str(results[0]["id"])


async def _create_tenant(client: httpx.AsyncClient, slug: str, name: str) -> dict:
    """Create the Nautobot Tenant — idempotent: re-use an existing one
    with the same *name* rather than 409-ing on re-runs.

    Nautobot 2.x removed the writable ``slug`` field from Tenant — only
    ``name`` (required, unique) and an auto-generated read-only
    ``natural_slug`` remain. We look up by name and pass the daalu slug
    only into the ``description`` (so operators browsing Nautobot can
    still match a Tenant row back to the daalu tenant by slug).
    """
    existing = await client.get("/api/tenancy/tenants/", params={"name": name})
    _raise_clean(existing, what=f"tenant lookup for {name!r}")
    results = (existing.json() or {}).get("results") or []
    if results:
        return results[0]
    resp = await client.post(
        "/api/tenancy/tenants/",
        json={"name": name, "description": f"daalu tenant {slug}"},
    )
    if resp.status_code not in (200, 201):
        raise ProvisioningError(
            f"Nautobot rejected tenant create for {slug!r}: "
            f"HTTP {resp.status_code} — {resp.text[:200]}"
        )
    return resp.json()


async def _create_permission(
    client: httpx.AsyncClient,
    *,
    tenant_slug: str,
    nautobot_tenant_id: str,
    user_id: str,
) -> dict:
    """Create an ObjectPermission constrained to the tenant.

    This is the *only* gate between customer A and customer B's data —
    the constraint dict must filter by the tenant relationship on every
    granted model. Nautobot evaluates constraints as ORM-style Django
    filters; we pin to the Nautobot Tenant's UUID: ``{"tenant":
    "<uuid>"}`` means "only rows whose .tenant_id equals this UUID".

    Using the UUID rather than ``tenant__slug`` is mandatory on
    Nautobot 2.x (the Tenant model no longer has a writable ``slug``
    field), and is strictly better even on 1.x — it's stable across
    Tenant renames in the Nautobot UI.

    Bound to the platform service user, scoped to GRANTED_OBJECT_TYPES,
    with GRANTED_ACTIONS. Idempotent: re-uses an existing permission
    with the same name rather than stacking.
    """
    name = f"daalu-tenant-{tenant_slug}"
    existing = await client.get("/api/users/permissions/", params={"name": name})
    _raise_clean(existing, what=f"permission lookup for {name!r}")
    results = (existing.json() or {}).get("results") or []
    if results:
        return results[0]
    body = {
        "name": name,
        "enabled": True,
        "object_types": list(GRANTED_OBJECT_TYPES),
        "actions": list(GRANTED_ACTIONS),
        # The constraint is the security boundary. Keep it explicit
        # and minimal — Nautobot evaluates it on every authorized
        # request, so a typo here would either lock the user out of
        # their own data (visible) or expose them to others (silent —
        # the test suite is the only thing that catches this).
        "constraints": {"tenant": nautobot_tenant_id},
        "users": [user_id],
        "groups": [],
    }
    resp = await client.post("/api/users/permissions/", json=body)
    if resp.status_code not in (200, 201):
        raise ProvisioningError(
            f"Nautobot rejected ObjectPermission create for {tenant_slug!r}: "
            f"HTTP {resp.status_code} — {resp.text[:200]}"
        )
    return resp.json()


async def _mint_token(
    client: httpx.AsyncClient, *, user_id: str, tenant_slug: str
) -> dict:
    """Mint an APIToken for the service user.

    Nautobot doesn't (in stock) let you scope a token to a subset of
    the user's permissions — the token carries the *user's* effective
    permissions at the time of each request. The user we attach to is
    the platform service user, whose only ObjectPermissions are the
    per-tenant ones we created. So the token's reach is constrained by
    the union of those — fine for one tenant; if we later add a second
    permission to the same user the boundary changes.

    Description includes the tenant slug so an operator browsing the
    Tokens table can tell which tenant a stray token belongs to.
    """
    resp = await client.post(
        "/api/users/tokens/",
        json={
            "user": user_id,
            "description": f"daalu tenant {tenant_slug}",
            "write_enabled": True,
        },
    )
    if resp.status_code not in (200, 201):
        raise ProvisioningError(
            f"Nautobot rejected token mint for {tenant_slug!r}: "
            f"HTTP {resp.status_code} — {resp.text[:200]}"
        )
    return resp.json()


async def provision_tenant(
    *,
    tenant_slug: str,
    tenant_name: str,
    managed_url: str | None = None,
    admin_token: str | None = None,
    service_user: str | None = None,
) -> ProvisionedNautobot:
    """Idempotently provision Nautobot Tenant + ObjectPermission + APIToken.

    Reads :data:`Settings.managed_nautobot_*` by default; the kwargs
    are an injection seam for testing. Raises
    :class:`ProvisioningError` if any sub-call fails — partial state
    (e.g., Tenant created but ObjectPermission failed) is *not*
    automatically rolled back, because subsequent re-runs are
    idempotent (find-or-create on slug / name).
    """
    s = get_settings()
    url = managed_url or s.managed_nautobot_url
    token = admin_token or s.managed_nautobot_admin_token
    service = service_user or s.managed_nautobot_service_user
    if not url or not token:
        raise ProvisioningError(
            "managed Nautobot provisioning is not configured — set "
            "managed_nautobot_url and managed_nautobot_admin_token in "
            "settings or pass them explicitly"
        )

    async with _build_admin_client(url, token) as client:
        user_id = await _find_user_id(client, service)
        tenant_row = await _create_tenant(client, tenant_slug, tenant_name)
        perm_row = await _create_permission(
            client,
            tenant_slug=tenant_slug,
            nautobot_tenant_id=str(tenant_row["id"]),
            user_id=user_id,
        )
        token_row = await _mint_token(
            client, user_id=user_id, tenant_slug=tenant_slug
        )

    cleartext = token_row.get("key")
    if not cleartext:
        # Nautobot has been known to omit `key` on token re-fetch; for
        # provisioning the *create* response must carry it or we have
        # nothing to give the customer.
        raise ProvisioningError(
            f"Nautobot returned no token key for {tenant_slug!r}; "
            "cannot complete provisioning"
        )
    return ProvisionedNautobot(
        url=url,
        token=cleartext,
        tenant_slug=tenant_slug,
        tenant_id=str(tenant_row["id"]),
        permission_id=str(perm_row["id"]),
    )
