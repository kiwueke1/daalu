"""The copilot must answer infra questions from the real SoT, not from memory.

These tests are deliberately DB-free: they patch ``AsyncSessionLocal`` and
``NautobotSoT`` so the tool handlers can be exercised without Postgres, and
they patch ``chat_with_tools`` so the copilot's turn loop can be driven
without an LLM. What is under test is the wiring — that the tools exist,
that they are reachable from the command bar, that only read tools are, and
that tool output actually reaches the model.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from daalu_automation.core import kube_tools, sot_tools

TENANT = uuid.uuid4()


def _device(name, ip, platform="cumulus-linux", tags=("leaf",)):
    return SimpleNamespace(
        id=str(uuid.uuid4()),
        name=name,
        primary_ip=ip,
        platform=platform,
        transport="linux_ssh",
        tags=list(tags),
        extra={},
    )


FABRIC = [
    _device("spine1", "172.20.20.11/24", tags=("spine",)),
    _device("spine2", "172.20.20.12/24", tags=("spine",)),
    _device("leaf1", "172.20.20.21/24", tags=("leaf",)),
    _device("gpu1", "172.20.20.31/24", platform="linux", tags=("server",)),
]


@pytest.fixture
def fake_sot(monkeypatch):
    """Patch the SoT + session so handlers run without Postgres/Nautobot."""

    class FakeSoT:
        def __init__(self, env=None):
            self.env = env

        async def list_devices(self, db, tenant_id, *, platform=None):
            return [d for d in FABRIC if platform is None or d.platform == platform]

        async def get_device(self, db, tenant_id, device_id):
            return next((d for d in FABRIC if d.id == device_id), None)

    @asynccontextmanager
    async def fake_session():
        yield object()

    import daalu_automation.core.sot as sot_mod
    import daalu_automation.database as db_mod

    monkeypatch.setattr(sot_mod, "NautobotSoT", FakeSoT)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", fake_session)
    return FakeSoT


# --------------------------------------------------------------------------
# The tools exist and are registered
# --------------------------------------------------------------------------


def test_sot_read_tools_are_registered():
    """propose_change's schema has always told the model to 'look it up via
    list_devices'. That tool has to actually exist."""
    for name in ("list_devices", "get_device", "get_fabric_topology"):
        assert name in kube_tools.TOOLS, f"{name} not in the tool registry"
        assert not kube_tools.tool_requires_approval(name), f"{name} should be read-only"


def test_copilot_exposes_only_read_tools():
    from daalu_automation.api.routers.copilot import _tool_definitions

    exposed = {d["function"]["name"] for d in _tool_definitions()}
    assert "list_devices" in exposed
    for mutating in (
        "delete_pod",
        "restart_deployment",
        "scale_deployment",
        "patch_resource",
        "rollout_undo",
        "propose_change",
        "call_external_api",
    ):
        assert mutating not in exposed, f"write tool {mutating} reachable from copilot"


def test_allowlist_matches_registry():
    """A typo in the allowlist would silently drop a tool."""
    from daalu_automation.api.routers.copilot import _COPILOT_TOOLS

    unknown = _COPILOT_TOOLS - set(kube_tools.TOOLS)
    assert not unknown, f"allowlisted tools missing from registry: {sorted(unknown)}"


# --------------------------------------------------------------------------
# The handlers return usable answers
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_devices_returns_real_inventory(fake_sot):
    out = await sot_tools._list_devices(_tenant_id=TENANT)
    assert "4 device(s)" in out
    assert "spine1" in out and "172.20.20.11/24" in out
    assert "gpu1" in out


@pytest.mark.asyncio
async def test_list_devices_filters_by_role(fake_sot):
    out = await sot_tools._list_devices(role="spine", _tenant_id=TENANT)
    assert "spine1" in out and "spine2" in out
    assert "leaf1" not in out


@pytest.mark.asyncio
async def test_list_devices_filters_by_platform(fake_sot):
    out = await sot_tools._list_devices(platform="linux", _tenant_id=TENANT)
    assert "gpu1" in out
    assert "spine1" not in out


@pytest.mark.asyncio
async def test_get_device_resolves_by_name_not_just_uuid(fake_sot):
    """Users say 'leaf1'. The model relays that, so name lookup must work."""
    out = await sot_tools._get_device(device="leaf1", _tenant_id=TENANT)
    assert "leaf1" in out
    assert "172.20.20.21/24" in out


@pytest.mark.asyncio
async def test_get_device_is_case_insensitive(fake_sot):
    out = await sot_tools._get_device(device="LEAF1", _tenant_id=TENANT)
    assert "172.20.20.21/24" in out


@pytest.mark.asyncio
async def test_get_device_unknown_name_is_an_honest_error(fake_sot):
    out = await sot_tools._get_device(device="nope99", _tenant_id=TENANT)
    assert out.startswith("error:")
    assert "list_devices" in out


@pytest.mark.asyncio
async def test_missing_tenant_is_an_error_not_a_crash():
    out = await sot_tools._list_devices(_tenant_id=None)
    assert out.startswith("error:")


@pytest.mark.asyncio
async def test_empty_sot_says_so_rather_than_inventing(monkeypatch):
    """A freshly provisioned environment has no devices. The model must be
    told that plainly — this is the exact moment it would otherwise guess."""

    class EmptySoT:
        def __init__(self, env=None):
            pass

        async def list_devices(self, db, tenant_id, *, platform=None):
            return []

    @asynccontextmanager
    async def fake_session():
        yield object()

    import daalu_automation.core.sot as sot_mod
    import daalu_automation.database as db_mod

    monkeypatch.setattr(sot_mod, "NautobotSoT", EmptySoT)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", fake_session)

    out = await sot_tools._list_devices(_tenant_id=TENANT)
    assert "No devices matched" in out
    assert "import" in out.lower()


@pytest.mark.asyncio
async def test_unreachable_sot_reports_the_failure(monkeypatch):
    from daalu_automation.core.sot import NautobotUnavailable

    class DeadSoT:
        def __init__(self, env=None):
            pass

        async def list_devices(self, db, tenant_id, *, platform=None):
            raise NautobotUnavailable("connection refused")

    @asynccontextmanager
    async def fake_session():
        yield object()

    import daalu_automation.core.sot as sot_mod
    import daalu_automation.database as db_mod

    monkeypatch.setattr(sot_mod, "NautobotSoT", DeadSoT)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", fake_session)

    out = await sot_tools._list_devices(_tenant_id=TENANT)
    assert out.startswith("error:")
    assert "not reachable" in out


# --------------------------------------------------------------------------
# The copilot turn loop
# --------------------------------------------------------------------------


class _FakeDB:
    """Minimal stand-in for the AsyncSession the router uses for events/alerts."""

    async def execute(self, *_a, **_kw):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: []), all=lambda: []
        )


@pytest.mark.asyncio
async def test_copilot_calls_tools_and_answers_from_their_output(monkeypatch):
    """The whole point: a question about infra must reach a tool, and the
    tool's output must come back into the model's next turn."""
    from daalu_automation.api.routers import copilot

    turns = []
    executed = []

    async def fake_chat(*, system, messages, tools, **kw):
        turns.append(messages)
        if len(turns) == 1:
            assert any(t["function"]["name"] == "list_devices" for t in tools)
            return {
                "text": "",
                "tool_calls": [
                    {"id": "c1", "name": "list_devices", "arguments": {"role": "spine"}}
                ],
                "finish_reason": "tool_calls",
            }
        return {
            "text": "You have 2 spines: spine1 and spine2.",
            "tool_calls": [],
            "finish_reason": "stop",
        }

    async def fake_execute(name, args, *, tenant_id=None, **kw):
        executed.append((name, args))
        return "2 device(s):\nspine1 | 172.20.20.11/24\nspine2 | 172.20.20.12/24"

    monkeypatch.setattr(copilot, "chat_with_tools", fake_chat)
    monkeypatch.setattr(copilot.kube_tools, "execute_tool", fake_execute)

    resp = await copilot.ask(
        SimpleNamespace(query="how many spines do I have?"),
        db=_FakeDB(),
        tenant_id=TENANT,
    )

    assert executed == [("list_devices", {"role": "spine"})]
    assert "spine1" in resp.answer
    # The tool result must be present in the second turn's messages.
    second = turns[1]
    assert any(
        m.get("role") == "tool" and "172.20.20.11/24" in m.get("content", "")
        for m in second
    ), "tool output never reached the model"


@pytest.mark.asyncio
async def test_copilot_answers_without_tools_when_none_needed(monkeypatch):
    from daalu_automation.api.routers import copilot

    async def fake_chat(*, system, messages, tools, **kw):
        return {"text": "Nothing is on fire.", "tool_calls": [], "finish_reason": "stop"}

    monkeypatch.setattr(copilot, "chat_with_tools", fake_chat)
    resp = await copilot.ask(
        SimpleNamespace(query="anything wrong?"), db=_FakeDB(), tenant_id=TENANT
    )
    assert resp.answer == "Nothing is on fire."


@pytest.mark.asyncio
async def test_copilot_stops_looping_and_still_answers(monkeypatch):
    """A model that keeps calling tools must not hang or return nothing."""
    from daalu_automation.api.routers import copilot

    calls = {"n": 0}

    async def fake_chat(*, system, messages, tools, **kw):
        calls["n"] += 1
        if tools:
            return {
                "text": "",
                "tool_calls": [
                    {"id": f"c{calls['n']}", "name": "list_devices", "arguments": {}}
                ],
                "finish_reason": "tool_calls",
            }
        return {"text": "Final answer.", "tool_calls": [], "finish_reason": "stop"}

    async def fake_execute(name, args, *, tenant_id=None, **kw):
        return "some devices"

    monkeypatch.setattr(copilot, "chat_with_tools", fake_chat)
    monkeypatch.setattr(copilot.kube_tools, "execute_tool", fake_execute)

    resp = await copilot.ask(
        SimpleNamespace(query="loop forever"), db=_FakeDB(), tenant_id=TENANT
    )
    assert resp.answer == "Final answer."
    assert calls["n"] == copilot._MAX_TOOL_ROUNDS + 1


@pytest.mark.asyncio
async def test_copilot_offline_message_does_not_blame_anthropic(monkeypatch):
    """chat_with_tools skips the Anthropic tier entirely, so telling the user
    to set ANTHROPIC_API_KEY (as the old copilot did) sends them to a knob
    that cannot fix this path."""
    from daalu_automation.api.routers import copilot
    from daalu_automation.core.llm import LLMUnavailable

    async def dead(*a, **kw):
        raise LLMUnavailable("no tier")

    monkeypatch.setattr(copilot, "chat_with_tools", dead)
    resp = await copilot.ask(
        SimpleNamespace(query="hi"), db=_FakeDB(), tenant_id=TENANT
    )
    assert "ANTHROPIC_API_KEY" not in resp.answer
    assert "LLM_API_KEY" in resp.answer


# --------------------------------------------------------------------------
# Nautobot role extraction
#
# Prod devices carry their role in Nautobot's `role` field, not in tags. The
# GraphQL query never asked for it, so `extra["role"]` was always empty —
# which silently broke the copilot's role filter AND made topology's
# `_tier_for` fall back to guessing the tier from the device name.
# --------------------------------------------------------------------------


def _raw(name, role=None, cfd=None):
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "primary_ip4": {"address": "172.20.20.11/24"},
        "platform": {"name": "linux"},
        "role": {"name": role} if role else None,
        "tags": [],
        "custom_field_data": cfd or {},
    }


def test_device_query_asks_for_role():
    """A regression guard: dropping the field silently empties extra['role']."""
    from daalu_automation.core.sot import nautobot as nb

    assert "role { name }" in nb._DEVICES_QUERY
    assert "role { name }" in nb._DEVICE_QUERY


def test_to_device_populates_extra_role():
    from daalu_automation.core.sot.nautobot import NautobotSoT

    dev = NautobotSoT()._to_device(_raw("spine1", role="spine"))
    assert dev.extra.get("role") == "spine"


def test_custom_field_role_wins_over_nautobot_role():
    """An operator who defined a `role` custom field meant that one."""
    from daalu_automation.core.sot.nautobot import NautobotSoT

    dev = NautobotSoT()._to_device(
        _raw("spine1", role="spine", cfd={"role": "custom-thing"})
    )
    assert dev.extra["role"] == "custom-thing"


def test_missing_role_does_not_crash():
    from daalu_automation.core.sot.nautobot import NautobotSoT

    dev = NautobotSoT()._to_device(_raw("mystery", role=None))
    assert "role" not in dev.extra or not dev.extra.get("role")


@pytest.mark.asyncio
async def test_role_filter_works_off_extra_not_just_tags(monkeypatch):
    """The prod shape: role in extra, tags empty. This is what actually ships."""

    def _dev_with_role(name, role):
        return SimpleNamespace(
            id=str(uuid.uuid4()),
            name=name,
            primary_ip="172.20.20.11/24",
            platform="linux",
            transport="linux_ssh",
            tags=[],
            extra={"role": role},
        )

    fabric = [
        _dev_with_role("spine1", "spine"),
        _dev_with_role("leaf1", "leaf"),
        _dev_with_role("gpu1", "server"),
    ]

    class RoleSoT:
        def __init__(self, env=None):
            pass

        async def list_devices(self, db, tenant_id, *, platform=None):
            return fabric

    @asynccontextmanager
    async def fake_session():
        yield object()

    import daalu_automation.core.sot as sot_mod
    import daalu_automation.database as db_mod

    monkeypatch.setattr(sot_mod, "NautobotSoT", RoleSoT)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", fake_session)

    out = await sot_tools._list_devices(role="spine", _tenant_id=TENANT)
    assert "spine1" in out
    assert "leaf1" not in out and "gpu1" not in out


# --------------------------------------------------------------------------
# get_fabric_topology
#
# The first cut read a_device/a_interface/b_device/b_interface off
# TopologyLink. Those fields do not exist — the real ones are
# source/source_if/target/target_if — so every row rendered as "?:? <-> ?:?"
# and the model dutifully reported it could not determine the cabling. It
# also filtered management links on a `kind` attribute that does not exist,
# so mgmt noise (every device seeing every other on the OOB bridge) was
# never excluded.
# --------------------------------------------------------------------------


class _Link:
    def __init__(self, source, source_if, target, target_if, status="up"):
        self.source, self.source_if = source, source_if
        self.target, self.target_if = target, target_if
        self.status = status


class _Node:
    def __init__(self, id, name):
        self.id, self.name = id, name


class _Graph:
    def __init__(self, nodes, links):
        self.nodes, self.links = nodes, links


@pytest.fixture
def fake_graph(monkeypatch):
    nodes = [_Node("id-leaf1", "leaf1"), _Node("id-spine1", "spine1"), _Node("id-gpu1", "gpu1")]
    links = [
        _Link("id-leaf1", "swp11", "id-spine1", "swp1", "up"),
        _Link("id-leaf1", "swp1", "id-gpu1", "eth1", "up"),
        _Link("id-leaf1", "eth0", "id-spine1", "eth0", "mgmt"),
    ]

    async def fake_build(db, tenant_id, env=None):
        return _Graph(nodes, links)

    @asynccontextmanager
    async def fake_session():
        yield object()

    import daalu_automation.core.topology.service as svc
    import daalu_automation.database as db_mod

    monkeypatch.setattr(svc, "build_topology", fake_build)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", fake_session)


@pytest.mark.asyncio
async def test_topology_resolves_device_names_not_ids(fake_graph):
    out = await sot_tools._get_fabric_topology(_tenant_id=TENANT)
    assert "leaf1:swp11 <-> spine1:swp1" in out
    assert "?" not in out, "device ids were not mapped to names"
    assert "id-leaf1" not in out


@pytest.mark.asyncio
async def test_topology_excludes_mgmt_links_by_default(fake_graph):
    out = await sot_tools._get_fabric_topology(_tenant_id=TENANT)
    assert "2 fabric link(s)" in out
    assert "mgmt" not in out.replace("management links excluded", "")


@pytest.mark.asyncio
async def test_topology_can_include_mgmt_when_asked(fake_graph):
    out = await sot_tools._get_fabric_topology(include_mgmt=True, _tenant_id=TENANT)
    assert "3 fabric link(s)" in out


@pytest.mark.asyncio
async def test_topology_filters_by_device_name(fake_graph):
    out = await sot_tools._get_fabric_topology(device="gpu1", _tenant_id=TENANT)
    assert "gpu1:eth1" in out
    assert "spine1:swp1 " not in out


@pytest.mark.asyncio
async def test_topology_unknown_device_is_an_honest_error(fake_graph):
    out = await sot_tools._get_fabric_topology(device="nope", _tenant_id=TENANT)
    assert "No fabric links found" in out


# --------------------------------------------------------------------------
# Cluster targeting: answer about the cluster the user means
# --------------------------------------------------------------------------


def _patch_clusters(monkeypatch, copilot, slugs):
    async def fake_ctx(_db, _tenant):
        return slugs, [f"- {s} ({s}; connected) — metrics: none, logs: none" for s in slugs]

    monkeypatch.setattr(copilot, "_clusters_context", fake_ctx)


async def _ask_with_one_call(
    monkeypatch, copilot, slugs, *, call_args, viewing=None,
    query="what pods are running?",
):
    """Drive one tool round (list_pods with ``call_args``) and capture what
    reached execute_tool and what went back to the model."""
    _patch_clusters(monkeypatch, copilot, slugs)
    executed, turns = [], []

    async def fake_chat(*, system, messages, tools, **kw):
        turns.append(messages)
        if len(turns) == 1:
            return {
                "text": "",
                "tool_calls": [{"id": "c1", "name": "list_pods", "arguments": call_args}],
                "finish_reason": "tool_calls",
            }
        return {"text": "done", "tool_calls": [], "finish_reason": "stop"}

    async def fake_execute(name, args, *, tenant_id=None, cluster_slug=None, **kw):
        executed.append((name, args, cluster_slug))
        return "pod-a Running"

    monkeypatch.setattr(copilot, "chat_with_tools", fake_chat)
    monkeypatch.setattr(copilot.kube_tools, "execute_tool", fake_execute)
    await copilot.ask(
        SimpleNamespace(query=query, cluster=viewing),
        db=_FakeDB(),
        tenant_id=TENANT,
    )
    tool_msgs = [m for m in turns[1] if m.get("role") == "tool"]
    return executed, tool_msgs[0]["content"], turns[0]


def test_per_cluster_tools_take_a_cluster_argument():
    from daalu_automation.api.routers import copilot

    defs = {d["function"]["name"]: d["function"] for d in copilot._tool_definitions()}
    assert "cluster" in defs["list_pods"]["parameters"]["properties"]
    assert "cluster" in defs["query_prometheus"]["parameters"]["properties"]
    assert "cluster" not in defs["list_devices"]["parameters"].get("properties", {})


@pytest.mark.asyncio
async def test_copilot_defaults_to_the_cluster_being_viewed(monkeypatch):
    from daalu_automation.api.routers import copilot

    executed, result, first_turn = await _ask_with_one_call(
        monkeypatch, copilot, ["daalu-prod", "daalu-demo"],
        call_args={"namespace": "default"}, viewing="daalu-demo",
    )
    assert executed == [("list_pods", {"namespace": "default"}, "daalu-demo")]
    assert result.startswith("[cluster: daalu-demo]")
    assert "VIEWING: daalu-demo" in first_turn[0]["content"]


@pytest.mark.asyncio
async def test_a_cluster_the_user_names_wins_over_the_viewed_one(monkeypatch):
    from daalu_automation.api.routers import copilot

    executed, _result, _ = await _ask_with_one_call(
        monkeypatch, copilot, ["daalu-prod", "daalu-demo"],
        call_args={"cluster": "daalu-prod"}, viewing="daalu-demo",
        query="what pods are running on daalu-prod?",
    )
    assert executed == [("list_pods", {}, "daalu-prod")]


@pytest.mark.asyncio
async def test_model_cannot_swap_the_viewed_cluster_for_another(monkeypatch):
    """Seen live: on daalu-demo's page, "this cluster" got answered from
    daalu-prod because the model passed cluster=daalu-prod on its own."""
    from daalu_automation.api.routers import copilot

    executed, result, _ = await _ask_with_one_call(
        monkeypatch, copilot, ["daalu-prod", "daalu-demo"],
        call_args={"cluster": "daalu-prod"}, viewing="daalu-demo",
        query="what pods are running on this cluster?",
    )
    assert executed == [("list_pods", {}, "daalu-demo")]
    assert result.startswith("[cluster: daalu-demo]")


@pytest.mark.asyncio
async def test_unviewed_guess_is_refused_with_several_clusters(monkeypatch):
    from daalu_automation.api.routers import copilot

    executed, result, _ = await _ask_with_one_call(
        monkeypatch, copilot, ["daalu-prod", "daalu-demo"],
        call_args={"cluster": "daalu-prod"},
    )
    assert executed == []
    assert "didn't say which one" in result


@pytest.mark.asyncio
async def test_several_clusters_and_no_target_is_not_guessed(monkeypatch):
    """With more than one cluster and none named or viewed, the copilot must
    not silently answer about the first one."""
    from daalu_automation.api.routers import copilot

    executed, result, _ = await _ask_with_one_call(
        monkeypatch, copilot, ["daalu-prod", "daalu-demo"], call_args={}
    )
    assert executed == []
    assert "didn't say which one" in result and "daalu-demo" in result


@pytest.mark.asyncio
async def test_unknown_cluster_is_an_honest_error(monkeypatch):
    from daalu_automation.api.routers import copilot

    executed, result, _ = await _ask_with_one_call(
        monkeypatch, copilot, ["daalu-prod"], call_args={"cluster": "nope"}
    )
    assert executed == []
    assert "no cluster named 'nope'" in result


@pytest.mark.asyncio
async def test_single_cluster_needs_no_target(monkeypatch):
    from daalu_automation.api.routers import copilot

    executed, _result, _ = await _ask_with_one_call(
        monkeypatch, copilot, ["daalu-prod"], call_args={}
    )
    assert executed == [("list_pods", {}, "daalu-prod")]
