"""The importer fills in the catalog a new environment doesn't have yet.

Every NV-CM environment is provisioned with a Nautobot that has no locations at
all and only NVIDIA's reference device types, so the first inventory upload
into each new environment failed all of its rows with "unknown site /
device_type / role / platform" — a file that is perfectly correct, refused by
an empty catalog. The only fix used to be a seeding script hardcoded to one
lab, so the next environment hit it again.

What's pinned here:

1. The plan is the diff between the file's references and the catalog — no
   duplicates, no re-creating what already resolves, and the parent prefix of
   each primary IP included (an IP with no covering prefix is what leaves a
   device with no primary IP).
2. A planned name makes its row valid-with-a-note instead of an error, so the
   preview shows what will happen rather than a wall of red.
3. Creation walks the location-type chain: a Site whose type demands a parent
   gets one, because "A Location of type Site must have a parent Location"
   reaches the operator as the far less useful "unknown site".
4. A catalog write that fails leaves the run going and says why.
"""

from __future__ import annotations

import json

import httpx
import pytest

from daalu_automation.core.sot.bulk_import import ParsedRow
from daalu_automation.core.sot.catalog_ensure import (
    CatalogPlan,
    ensure_catalog,
    parent_prefix,
    plan_catalog_additions,
)


def _row(
    idx: int = 1,
    *,
    name: str = "leaf1",
    primary_ip: str = "172.20.30.21/24",
    site: str = "lab-cp03-ncpain",
    device_type: str = "cumulus-vx",
    role: str = "leaf",
    platform: str | None = None,
    parse_error: str | None = None,
) -> ParsedRow:
    return ParsedRow(
        row_index=idx,
        name=name,
        primary_ip=primary_ip,
        transport="linux_ssh",
        site=site,
        device_type=device_type,
        role=role,
        platform=platform,
        parse_error=parse_error,
    )


# ---------------------------------------------------------------------------
# 1. Planning
# ---------------------------------------------------------------------------


def test_plan_is_the_diff_against_the_existing_catalog():
    plan = plan_catalog_additions(
        [
            _row(1, name="spine1", site="lab", device_type="cumulus-vx", role="spine"),
            _row(2, name="leaf1", site="lab", device_type="cumulus-vx", role="leaf"),
        ],
        # The site already exists; nothing else does.
        sites={"lab": "s1"},
        device_types={},
        roles={},
        platforms={},
        prefixes=set(),
    )
    assert plan.sites == []
    # Deduped, in file order — the same type named twice is created once.
    assert plan.device_types == ["cumulus-vx"]
    assert plan.roles == ["spine", "leaf"]


def test_plan_includes_the_parent_prefix_each_ip_needs():
    plan = plan_catalog_additions(
        [
            _row(1, primary_ip="172.20.30.11/24"),
            _row(2, primary_ip="172.20.30.12/24"),
            _row(3, primary_ip="10.0.0.5/24"),
        ],
        sites={"lab-cp03-ncpain": "s1"},
        device_types={"cumulus-vx": "t1"},
        roles={"leaf": "r1"},
        platforms={},
        prefixes={"10.0.0.0/24"},
    )
    # Both /24 hosts collapse to one prefix, and the one Nautobot already holds
    # is left alone.
    assert plan.prefixes == ["172.20.30.0/24"]


def test_parent_prefix_handles_the_shapes_a_device_file_carries():
    assert parent_prefix("172.20.30.11/24") == "172.20.30.0/24"
    assert parent_prefix(" 10.1.2.3/16 ") == "10.1.0.0/16"
    # No mask: a host route, still a legal prefix to create.
    assert parent_prefix("10.1.2.3") == "10.1.2.3/32"
    assert parent_prefix("not-an-ip") is None
    assert parent_prefix("") is None


def test_unparseable_rows_contribute_nothing_to_the_plan():
    # A row we couldn't read is not a source of names to create objects from.
    plan = plan_catalog_additions(
        [_row(1, site="typo", role="tpyo", parse_error="missing 'name'")],
        sites={},
        device_types={},
        roles={},
        platforms={},
        prefixes=set(),
    )
    assert plan.is_empty


def test_plan_membership_is_case_insensitive():
    plan = CatalogPlan(roles=["Spine"])
    assert plan.has("roles", "spine")
    assert plan.has("roles", " SPINE ")
    assert not plan.has("roles", "leaf")


# ---------------------------------------------------------------------------
# 2. Row resolution against a plan
# ---------------------------------------------------------------------------


def test_a_planned_name_makes_the_row_valid_with_a_note():
    from daalu_automation.api.routers.sot_devices import _resolve_row

    plan = CatalogPlan(sites=["lab-cp03-ncpain"], roles=["leaf"])
    result, ids = _resolve_row(
        _row(device_type="cumulus-vx"),
        sites={},
        device_types={"cumulus-vx": "t1"},
        roles={},
        platforms={},
        plan=plan,
    )
    assert result.status == "valid"
    assert result.error is None
    assert result.warnings == [
        "will create site 'lab-cp03-ncpain'",
        "will create role 'leaf'",
    ]
    # No IDs yet — the apply phase re-resolves once the objects exist.
    assert ids is None


def test_a_name_that_is_not_planned_is_still_an_error():
    from daalu_automation.api.routers.sot_devices import _resolve_row

    result, ids = _resolve_row(
        _row(),
        sites={},
        device_types={},
        roles={},
        platforms={},
        plan=CatalogPlan(sites=["lab-cp03-ncpain"]),
    )
    assert result.status == "error"
    # The site is planned so it drops out; the rest still fail loudly.
    assert "site" not in (result.error or "")
    assert "unknown device_type 'cumulus-vx'" in (result.error or "")
    assert "unknown role 'leaf'" in (result.error or "")
    assert ids is None


def test_without_a_plan_nothing_changes():
    from daalu_automation.api.routers.sot_devices import _resolve_row

    result, ids = _resolve_row(
        _row(platform="linux"),
        sites={"lab-cp03-ncpain": "s1"},
        device_types={"cumulus-vx": "t1"},
        roles={"leaf": "r1"},
        platforms={"linux": "p1"},
    )
    assert result.status == "valid"
    assert result.warnings == []
    assert ids == {
        "site_id": "s1",
        "device_type_id": "t1",
        "device_role_id": "r1",
        "platform_id": "p1",
    }


# ---------------------------------------------------------------------------
# 3. Creation against a Nautobot
# ---------------------------------------------------------------------------


class _FakeNautobot:
    """Enough of Nautobot 2.x to exercise the create paths.

    Records every POST so the tests can assert on the bodies, and answers the
    list endpoints from what has been created so far — which is what makes the
    idempotency assertions meaningful.
    """

    def __init__(self, *, location_types: list[dict] | None = None, fail: str = ""):
        self.posts: list[tuple[str, dict]] = []
        self.fail = fail
        self.store: dict[str, list[dict]] = {
            "/api/dcim/locations/": [],
            "/api/dcim/location-types/": (
                location_types
                if location_types is not None
                else [
                    {"id": "lt-region", "name": "Region", "parent": None},
                    {
                        "id": "lt-site",
                        "name": "Site",
                        "parent": {"id": "lt-region", "name": "Region"},
                    },
                ]
            ),
            "/api/dcim/manufacturers/": [],
            "/api/dcim/device-types/": [],
            "/api/extras/roles/": [],
            "/api/dcim/platforms/": [],
            "/api/ipam/prefixes/": [],
        }

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET":
            return httpx.Response(200, json={"results": self.store.get(path, [])})
        body = json.loads(req.content) if req.content else {}
        self.posts.append((path, body))
        if self.fail and self.fail in path:
            return httpx.Response(400, text="nope")
        obj = dict(body)
        obj["id"] = f"{path.strip('/').split('/')[-1]}-{len(self.posts)}"
        # Echo the created object back into the list the module reads, so a
        # second pass sees it exactly as Nautobot would.
        self.store.setdefault(path, []).append(obj)
        return httpx.Response(201, json=obj)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://nautobot.test",
            transport=httpx.MockTransport(self.handler),
        )


@pytest.mark.asyncio
async def test_creating_a_site_walks_the_location_type_chain():
    fake = _FakeNautobot()
    async with fake.client() as client:
        result = await ensure_catalog(client, CatalogPlan(sites=["lab-cp03-ncpain"]))

    locations = [b for p, b in fake.posts if p == "/api/dcim/locations/"]
    # Region first (Nautobot rejects a Site with no parent), then the Site
    # hung off it.
    assert [loc["name"] for loc in locations] == [
        "lab-cp03-ncpain-region",
        "lab-cp03-ncpain",
    ]
    assert locations[0].get("parent") is None
    assert locations[1]["parent"] == "locations-1"
    assert result.errors == []
    assert "site 'lab-cp03-ncpain'" in result.created


@pytest.mark.asyncio
async def test_an_unambiguous_existing_ancestor_is_reused():
    fake = _FakeNautobot()
    fake.store["/api/dcim/locations/"] = [
        {"id": "reg-1", "name": "emea", "location_type": {"id": "lt-region"}}
    ]
    async with fake.client() as client:
        await ensure_catalog(client, CatalogPlan(sites=["lab2"]))

    locations = [b for p, b in fake.posts if p == "/api/dcim/locations/"]
    # One create, not two: the site joins the tree the operator already has.
    assert [loc["name"] for loc in locations] == ["lab2"]
    assert locations[0]["parent"] == "reg-1"


@pytest.mark.asyncio
async def test_existing_objects_are_never_recreated():
    fake = _FakeNautobot()
    fake.store["/api/dcim/locations/"] = [
        {"id": "s1", "name": "lab-cp03-ncpain", "location_type": {"id": "lt-site"}}
    ]
    # Nautobot displays "Cumulus Linux"; the file says "cumulus-linux". Same
    # object — the slugified match is what stops a duplicate platform.
    fake.store["/api/dcim/platforms/"] = [{"id": "p1", "name": "Cumulus Linux"}]
    fake.store["/api/ipam/prefixes/"] = [{"id": "x1", "prefix": "172.20.30.0/24"}]
    async with fake.client() as client:
        result = await ensure_catalog(
            client,
            CatalogPlan(
                sites=["lab-cp03-ncpain"],
                platforms=["cumulus-linux"],
                prefixes=["172.20.30.0/24"],
            ),
        )
    assert fake.posts == []
    assert result.created == []


@pytest.mark.asyncio
async def test_device_types_get_a_manufacturer_and_prefixes_a_namespace():
    fake = _FakeNautobot()
    async with fake.client() as client:
        await ensure_catalog(
            client,
            CatalogPlan(device_types=["gpu-server"], prefixes=["172.20.30.0/24"]),
        )
    by_path = dict(fake.posts)
    # Nautobot refuses a device type with no manufacturer, and a device file
    # never names one.
    assert by_path["/api/dcim/manufacturers/"]["name"] == "Generic"
    assert by_path["/api/dcim/device-types/"]["manufacturer"] == "manufacturers-1"
    assert by_path["/api/ipam/prefixes/"]["namespace"] == {"name": "Global"}


@pytest.mark.asyncio
async def test_roles_are_made_assignable_to_devices():
    fake = _FakeNautobot()
    async with fake.client() as client:
        await ensure_catalog(client, CatalogPlan(roles=["spine"]))
    body = dict(fake.posts)["/api/extras/roles/"]
    # A 2.x role is shared across models and only lands on a device once
    # dcim.device is in its content types.
    assert body["content_types"] == ["dcim.device"]


@pytest.mark.asyncio
async def test_a_failed_create_is_reported_not_raised():
    fake = _FakeNautobot(fail="platforms")
    async with fake.client() as client:
        result = await ensure_catalog(
            client, CatalogPlan(platforms=["linux"], roles=["spine"])
        )
    assert any("platform 'linux'" in e for e in result.errors)
    # The rest of the batch still went through — one bad object doesn't cost
    # the operator the whole import.
    assert "role 'spine'" in result.created


@pytest.mark.asyncio
async def test_a_nautobot_with_no_location_types_says_so():
    fake = _FakeNautobot(location_types=[])
    async with fake.client() as client:
        result = await ensure_catalog(client, CatalogPlan(sites=["lab"]))
    assert result.created == []
    assert any("location types" in e for e in result.errors)
