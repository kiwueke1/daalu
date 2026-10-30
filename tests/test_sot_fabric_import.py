"""Fabric-plan parsing, resolution and apply.

The three real lab files in ``examples/fabric-plan/`` are used as fixtures wherever
possible: they are what the feature exists to load, and they are generated from
the clab topology, so a drift between the generator and the importer shows up
here rather than on camera.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from daalu_automation.core.sot.fabric_apply import (
    FabricIndex,
    apply_rows,
    count_by_op,
    load_index,
    resolve_rows,
    summarize,
)
from daalu_automation.core.sot.fabric_import import (
    FabricParseError,
    detect_kind,
    parse_fabric_upload,
)

LAB = pathlib.Path(__file__).resolve().parents[1] / "examples" / "fabric-plan"


def _lab(artifact: str) -> bytes:
    return (LAB / f"fabric-{artifact}.yaml").read_bytes()


def _parse(artifact: str, expected: str | None = None):
    return parse_fabric_upload(f"fabric-{artifact}.yaml", None, _lab(artifact), expected=expected)


# ── Parsing ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("artifact", "expected_ops"),
    [
        ("cabling", {"interface": 71, "cable": 32}),
        ("addressing", {"prefix": 2, "address": 27}),
        ("intent", {"role": 7, "config_context": 3}),
    ],
)
def test_lab_files_parse_to_expected_object_counts(artifact, expected_ops):
    detected, rows = _parse(artifact)
    assert detected == artifact
    assert not [r for r in rows if r.parse_error]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.op] = counts.get(row.op, 0) + 1
    assert counts == expected_ops


def test_cabling_emits_interfaces_before_cables():
    """A cable can only terminate on an interface that already exists."""
    _, rows = _parse("cabling")
    ops = [r.op for r in rows]
    assert set(ops[: ops.count("interface")]) == {"interface"}
    assert set(ops[ops.count("interface") :]) == {"cable"}


def test_addressing_emits_prefixes_before_addresses():
    _, rows = _parse("addressing")
    ops = [r.op for r in rows]
    assert set(ops[: ops.count("prefix")]) == {"prefix"}


def test_cable_row_keeps_both_ends():
    _, rows = _parse("cabling")
    cable = next(r for r in rows if r.op == "cable" and r.target == "gpu1:eth1")
    assert cable.detail == "leaf1:swp1"
    assert cable.payload == {
        "a_device": "gpu1",
        "a_interface": "eth1",
        "b_device": "leaf1",
        "b_interface": "swp1",
    }


def test_wrong_tab_is_rejected_by_name():
    with pytest.raises(FabricParseError) as exc:
        _parse("intent", expected="cabling")
    assert "kind: intent" in str(exc.value)
    assert "cabling tab" in str(exc.value)


def test_kind_is_inferred_when_undeclared():
    assert detect_kind({"cables": []}) == "cabling"
    assert detect_kind({"loopbacks": []}) == "addressing"
    assert detect_kind({"config_contexts": []}) == "intent"


def test_file_spanning_two_artifacts_is_refused():
    """The pre-split combined plan would apply in an unenforceable order."""
    with pytest.raises(FabricParseError) as exc:
        detect_kind({"cables": [], "loopbacks": []})
    assert "separate files" in str(exc.value)


def test_unknown_kind_names_the_valid_ones():
    with pytest.raises(FabricParseError) as exc:
        detect_kind({"kind": "wiring", "cables": []})
    assert "cabling" in str(exc.value)


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        (b"", "empty"),
        (b"- just\n- a list\n", "mapping"),
        (b"kind: cabling\ninterfaces: {}\ncables: []\n", "no rows"),
    ],
)
def test_unusable_files_raise_rather_than_preview_nothing(payload, fragment):
    with pytest.raises(FabricParseError) as exc:
        parse_fabric_upload("x.yaml", None, payload)
    assert fragment in str(exc.value)


def test_malformed_rows_are_row_errors_not_file_errors():
    """One bad cable must not cost the other thirty-one."""
    doc = b"""
kind: cabling
cables:
  - [leaf1:swp1, gpu1:eth1]
  - [leaf1-swp2, gpu2:eth1]
  - [leaf1:swp3]
"""
    _, rows = parse_fabric_upload("c.yaml", None, doc)
    assert [bool(r.parse_error) for r in rows] == [False, True, True]
    assert "device:interface form" in rows[1].parse_error
    assert "exactly two ends" in rows[2].parse_error


def test_bad_address_is_flagged_with_the_value():
    doc = b"kind: addressing\nloopbacks:\n  - {device: leaf1, interface: lo, address: 10.0.0.999/32}\n"
    _, rows = parse_fabric_upload("a.yaml", None, doc)
    assert "10.0.0.999/32" in rows[0].parse_error


# ── Resolution ───────────────────────────────────────────────────────────


def _index(**kw) -> FabricIndex:
    return FabricIndex(**kw)


def test_unknown_device_is_an_error_naming_it():
    _, rows = parse_fabric_upload(
        "c.yaml", None, b"kind: cabling\ninterfaces:\n  ghost:\n    - {name: swp1}\n"
    )
    [result] = resolve_rows(rows, _index())
    assert result.status == "error"
    assert "unknown device 'ghost'" in result.error


def test_cable_may_reference_an_interface_the_same_file_creates():
    """Mirrors the device import's 'will create site x' behaviour."""
    _, rows = parse_fabric_upload(
        "c.yaml",
        None,
        b"kind: cabling\n"
        b"interfaces:\n  leaf1:\n    - {name: swp1}\n  gpu1:\n    - {name: eth1}\n"
        b"cables:\n  - [leaf1:swp1, gpu1:eth1]\n",
    )
    results = resolve_rows(rows, _index(devices={"leaf1": "d1", "gpu1": "d2"}))
    cable = results[-1]
    assert cable.status == "valid"
    assert cable.error is None
    assert "will create interface leaf1:swp1" in cable.warnings


def test_occupied_termination_is_caught_in_the_preview():
    """The schema constraint the episode demonstrates, surfaced before writing."""
    _, rows = parse_fabric_upload(
        "c.yaml", None, b"kind: cabling\ncables:\n  - [leaf1:swp1, gpu1:eth1]\n"
    )
    index = _index(
        devices={"leaf1": "d1", "gpu1": "d2"},
        interfaces={
            ("leaf1", "swp1"): {"id": "i1", "cable": "existing-cable"},
            ("gpu1", "eth1"): {"id": "i2", "cable": None},
        },
    )
    [result] = resolve_rows(rows, index)
    assert result.status == "error"
    assert "leaf1:swp1 already has a cable" in result.error
    assert "exactly one" in result.error


def test_already_present_cable_is_skipped_not_re_created():
    _, rows = parse_fabric_upload(
        "c.yaml", None, b"kind: cabling\ncables:\n  - [leaf1:swp1, gpu1:eth1]\n"
    )
    index = _index(
        devices={"leaf1": "d1", "gpu1": "d2"},
        interfaces={
            ("leaf1", "swp1"): {"id": "i1", "cable": "c9"},
            ("gpu1", "eth1"): {"id": "i2", "cable": "c9"},
        },
        cable_pairs={frozenset({"i1", "i2"})},
    )
    [result] = resolve_rows(rows, index)
    assert result.status == "exists"
    assert summarize([result])["skipped"] == 1


def test_address_without_its_interface_points_at_the_cabling_file():
    _, rows = parse_fabric_upload(
        "a.yaml",
        None,
        b"kind: addressing\nloopbacks:\n  - {device: leaf1, interface: lo, address: 10.0.0.21/32}\n",
    )
    [result] = resolve_rows(rows, _index(devices={"leaf1": "d1"}))
    assert result.status == "error"
    assert "upload the cabling file first" in result.error


def test_existing_address_detached_from_its_interface_still_has_work():
    """An address object can exist while sitting on nothing.

    Calling that row `exists` would skip the assignment forever, leaving the
    loopback unusable and the render failing on a missing router-id.
    """
    _, rows = parse_fabric_upload(
        "a.yaml",
        None,
        b"kind: addressing\nloopbacks:\n  - {device: leaf1, interface: lo, address: 10.0.0.21/32}\n",
    )
    index = _index(
        devices={"leaf1": "d1"},
        interfaces={("leaf1", "lo"): {"id": "i1", "cable": None}},
        addresses={"10.0.0.21/32": "ip1"},
    )
    [result] = resolve_rows(rows, index)
    assert result.status == "valid"
    assert "will assign it to this interface" in result.warnings[0]


def test_address_already_on_this_interface_is_a_skip():
    _, rows = parse_fabric_upload(
        "a.yaml",
        None,
        b"kind: addressing\nloopbacks:\n  - {device: leaf1, interface: lo, address: 10.0.0.21/32}\n",
    )
    index = _index(
        devices={"leaf1": "d1"},
        interfaces={("leaf1", "lo"): {"id": "i1", "cable": None}},
        addresses={"10.0.0.21/32": "ip1"},
        assignments={"ip1": {"i1"}},
    )
    [result] = resolve_rows(rows, index)
    assert result.status == "exists"


@pytest.mark.asyncio
async def test_apply_reuses_an_existing_address_instead_of_re_creating_it():
    _, rows = parse_fabric_upload(
        "a.yaml",
        None,
        b"kind: addressing\nloopbacks:\n  - {device: leaf1, interface: lo, address: 10.0.0.21/32}\n",
    )
    index = _index(
        devices={"leaf1": "d1"},
        interfaces={("leaf1", "lo"): {"id": "i1", "cable": None}},
        addresses={"10.0.0.21/32": "ip1"},
    )
    resolved = resolve_rows(rows, index)
    fake = _FakeNautobot()
    async with fake.client() as client:
        applied = await apply_rows(client, rows, resolved, index)
    assert applied[0].status == "created"
    # Only the assignment — the address itself was reused.
    assert [p for p, _ in fake.posts] == ["/api/ipam/ip-address-to-interface/"]
    assert fake.posts[0][1] == {"ip_address": "ip1", "interface": "i1"}


def test_role_already_correct_is_a_skip():
    _, rows = parse_fabric_upload("i.yaml", None, b"kind: intent\nroles:\n  leaf1: Leaf-Common\n")
    index = _index(devices={"leaf1": "d1"}, device_roles={"d1": "Leaf-Common"})
    [result] = resolve_rows(rows, index)
    assert result.status == "exists"


def test_role_change_reports_what_it_was():
    _, rows = parse_fabric_upload("i.yaml", None, b"kind: intent\nroles:\n  leaf1: Leaf-Common\n")
    index = _index(devices={"leaf1": "d1"}, device_roles={"d1": "leaf"})
    [result] = resolve_rows(rows, index)
    assert result.status == "valid"
    assert "will create role 'Leaf-Common'" in result.warnings
    assert "was 'leaf'" in result.warnings


def test_context_may_scope_to_a_role_the_same_file_creates():
    doc = b"""
kind: intent
roles:
  leaf1: Leaf-Common
config_contexts:
  - name: leaf-common-intent
    scope: {role: Leaf-Common, location: lab-cp03}
    data: {fabric-mac: 'aa:bb'}
"""
    _, rows = parse_fabric_upload("i.yaml", None, doc)
    index = _index(devices={"leaf1": "d1"}, locations={"lab-cp03": "l1"})
    results = resolve_rows(rows, index)
    assert results[-1].status == "valid"
    assert results[-1].error is None


def test_context_with_an_unknown_location_errors():
    doc = b"""
kind: intent
config_contexts:
  - name: x
    scope: {role: Leaf-Common, location: nowhere}
    data: {a: 1}
"""
    _, rows = parse_fabric_upload("i.yaml", None, doc)
    [result] = resolve_rows(rows, _index(roles={"leaf-common": "r1"}))
    assert "unknown location 'nowhere'" in result.error


def test_count_by_op_reports_what_formed():
    _, rows = _parse("cabling")
    results = resolve_rows(
        rows,
        _index(
            devices={
                n: n
                for n in (
                    "spine1",
                    "spine2",
                    "leaf1",
                    "leaf2",
                    "leaf3",
                    "leaf4",
                    "storleaf1",
                    "gpu1",
                    "gpu2",
                    "gpu3",
                    "gpu4",
                    "stor1",
                )
            }
        ),
    )
    assert count_by_op(results) == {"interface": 71, "cable": 32}


# ── Apply ────────────────────────────────────────────────────────────────


class _FakeNautobot:
    """Records POSTs and hands back ids, so apply order can be asserted."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.patches: list[tuple[str, dict]] = []
        self._next = 0

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://nautobot.test",
            transport=httpx.MockTransport(self._handle),
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.method == "POST":
            self.posts.append((request.url.path, body))
            self._next += 1
            return httpx.Response(201, json={"id": f"new-{self._next}"})
        if request.method == "PATCH":
            self.patches.append((request.url.path, body))
            return httpx.Response(200, json={"id": "patched"})
        return httpx.Response(200, json={"results": []})


@pytest.mark.asyncio
async def test_apply_creates_interfaces_then_cables_wired_to_the_new_ids():
    _, rows = parse_fabric_upload(
        "c.yaml",
        None,
        b"kind: cabling\n"
        b"interfaces:\n  leaf1:\n    - {name: swp1, type: 10gbase-x-sfpp}\n"
        b"  gpu1:\n    - {name: eth1, type: 10gbase-x-sfpp}\n"
        b"cables:\n  - [leaf1:swp1, gpu1:eth1]\n",
    )
    index = _index(devices={"leaf1": "d1", "gpu1": "d2"})
    resolved = resolve_rows(rows, index)
    fake = _FakeNautobot()
    async with fake.client() as client:
        applied = await apply_rows(client, rows, resolved, index)

    assert [r.status for r in applied] == ["created", "created", "created"]
    paths = [p for p, _ in fake.posts]
    assert paths == [
        "/api/dcim/interfaces/",
        "/api/dcim/interfaces/",
        "/api/dcim/cables/",
    ]
    cable_body = fake.posts[-1][1]
    assert cable_body["termination_a_id"] == "new-1"
    assert cable_body["termination_b_id"] == "new-2"
    assert cable_body["termination_a_type"] == "dcim.interface"
    assert cable_body["status"] == "Connected"


@pytest.mark.asyncio
async def test_apply_skips_error_and_exists_rows():
    _, rows = parse_fabric_upload(
        "c.yaml",
        None,
        b"kind: cabling\ninterfaces:\n  ghost:\n    - {name: swp1}\n",
    )
    index = _index()
    resolved = resolve_rows(rows, index)
    fake = _FakeNautobot()
    async with fake.client() as client:
        applied = await apply_rows(client, rows, resolved, index)
    assert applied[0].status == "error"
    assert fake.posts == []


@pytest.mark.asyncio
async def test_apply_assigns_an_address_to_its_interface():
    _, rows = parse_fabric_upload(
        "a.yaml",
        None,
        b"kind: addressing\nloopbacks:\n  - {device: leaf1, interface: lo, address: 10.0.0.21/32}\n",
    )
    index = _index(
        devices={"leaf1": "d1"}, interfaces={("leaf1", "lo"): {"id": "i1", "cable": None}}
    )
    resolved = resolve_rows(rows, index)
    fake = _FakeNautobot()
    async with fake.client() as client:
        applied = await apply_rows(client, rows, resolved, index)
    assert applied[0].status == "created"
    assert [p for p, _ in fake.posts] == [
        "/api/ipam/ip-addresses/",
        "/api/ipam/ip-address-to-interface/",
    ]
    assert fake.posts[-1][1] == {"ip_address": "new-1", "interface": "i1"}


@pytest.mark.asyncio
async def test_apply_creates_a_missing_role_then_patches_the_device():
    _, rows = parse_fabric_upload("i.yaml", None, b"kind: intent\nroles:\n  leaf1: Leaf-Common\n")
    index = _index(devices={"leaf1": "d1"}, device_roles={"d1": "leaf"})
    resolved = resolve_rows(rows, index)
    fake = _FakeNautobot()
    async with fake.client() as client:
        applied = await apply_rows(client, rows, resolved, index)
    assert applied[0].status == "created"
    assert fake.posts[0][1]["name"] == "Leaf-Common"
    assert fake.patches == [("/api/dcim/devices/d1/", {"role": "new-1"})]


@pytest.mark.asyncio
async def test_apply_scopes_a_context_to_role_and_location():
    doc = b"""
kind: intent
config_contexts:
  - name: leaf-common-intent
    scope: {role: Leaf-Common, location: lab-cp03}
    data: {fabric-mac: 'aa:bb'}
"""
    _, rows = parse_fabric_upload("i.yaml", None, doc)
    index = _index(roles={"leaf-common": "r1"}, locations={"lab-cp03": "l1"})
    resolved = resolve_rows(rows, index)
    fake = _FakeNautobot()
    async with fake.client() as client:
        applied = await apply_rows(client, rows, resolved, index)
    assert applied[0].status == "created"
    body = fake.posts[0][1]
    assert body["roles"] == ["r1"]
    assert body["locations"] == ["l1"]
    assert body["data"] == {"fabric-mac": "aa:bb"}
    assert body["is_active"] is True


@pytest.mark.asyncio
async def test_apply_reports_a_failed_row_without_losing_the_rest():
    _, rows = parse_fabric_upload(
        "c.yaml",
        None,
        b"kind: cabling\ninterfaces:\n  leaf1:\n    - {name: swp1}\n    - {name: swp2}\n",
    )
    index = _index(devices={"leaf1": "d1"})
    resolved = resolve_rows(rows, index)

    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(400, text="swp1 is invalid")
        return httpx.Response(201, json={"id": "ok"})

    async with httpx.AsyncClient(
        base_url="https://nautobot.test", transport=httpx.MockTransport(handle)
    ) as client:
        applied = await apply_rows(client, rows, resolved, index)

    assert [r.status for r in applied] == ["error", "created"]
    assert "swp1 is invalid" in applied[0].error
    assert summarize(applied) == {
        "total": 2,
        "valid": 0,
        "created": 1,
        "skipped": 0,
        "errors": 1,
    }


def test_termination_id_reads_both_nautobot_spellings():
    """Flat `termination_a_id` and nested `termination_a: {id}` both occur."""
    from daalu_automation.core.sot.fabric_apply import _termination_id

    assert _termination_id({"termination_a_id": "i1"}, "a") == "i1"
    assert _termination_id({"termination_a": {"id": "i1"}}, "a") == "i1"
    assert _termination_id({"termination_a": None}, "a") is None
    assert _termination_id({}, "b") is None


@pytest.mark.asyncio
async def test_load_index_follows_pagination():
    """A truncated index blames the plan for the reader's page cap."""
    pages = {
        "/api/dcim/interfaces/": [
            {
                "results": [
                    {
                        "id": "i1",
                        "name": "swp1",
                        "device": {"id": "d1", "name": "leaf1"},
                        "cable": None,
                    }
                ],
                "next": "https://nautobot.test/api/dcim/interfaces/?offset=500",
            },
            {
                "results": [
                    {
                        "id": "i2",
                        "name": "swp2",
                        "device": {"id": "d1", "name": "leaf1"},
                        "cable": None,
                    }
                ],
                "next": None,
            },
        ]
    }
    seen: dict[str, int] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/dcim/devices/":
            return httpx.Response(200, json={"results": [{"id": "d1", "name": "leaf1"}]})
        if path in pages:
            n = seen.get(path, 0)
            seen[path] = n + 1
            return httpx.Response(200, json=pages[path][min(n, len(pages[path]) - 1)])
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(
        base_url="https://nautobot.test", transport=httpx.MockTransport(handle)
    ) as client:
        index = await load_index(client, "cabling")

    assert seen["/api/dcim/interfaces/"] == 2
    assert index.interface("leaf1", "swp1") is not None
    assert index.interface("leaf1", "swp2") is not None


@pytest.mark.asyncio
async def test_load_index_marks_occupied_terminations():
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/dcim/devices/":
            return httpx.Response(
                200, json={"results": [{"id": "d1", "name": "leaf1", "role": {"name": "leaf"}}]}
            )
        if path == "/api/dcim/interfaces/":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "i1",
                            "name": "swp1",
                            "device": {"id": "d1", "name": "leaf1"},
                            "cable": {"id": "c1"},
                        },
                        {
                            "id": "i2",
                            "name": "swp2",
                            "device": {"id": "d1", "name": "leaf1"},
                            "cable": None,
                        },
                    ]
                },
            )
        if path == "/api/dcim/cables/":
            return httpx.Response(
                200,
                json={"results": [{"id": "c1", "termination_a_id": "i1", "termination_b_id": "z"}]},
            )
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(
        base_url="https://nautobot.test", transport=httpx.MockTransport(handle)
    ) as client:
        index = await load_index(client, "cabling")

    assert index.devices == {"leaf1": "d1"}
    assert index.device_roles == {"d1": "leaf"}
    assert index.interface("leaf1", "swp1")["cable"] == "c1"
    assert index.interface("leaf1", "swp2")["cable"] is None
    assert frozenset({"i1", "z"}) in index.cable_pairs
