"""Host resolution for the hub's NV-CM tool proxy, with named environments.

Two properties matter here and neither is cosmetic:

* **Legacy hosts keep working.** ``<tenant>`` and ``<tool>-<tenant>`` must
  still resolve, now to the tenant's primary environment — those URLs are
  live and bookmarked.
* **Ownership is enforced on the resolved tenant.** The label is attacker-
  influenced (it's just a hostname), and a mis-resolution that also passed the
  ownership check would hand one customer another customer's consoles.

These run without a database: ``_resolve`` is exercised against a fake session
that answers the three queries it makes, so the resolution order (and the
absence of any cross-environment fallback) is testable directly.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from daalu_automation.api import tool_proxy as tp
from daalu_automation.core.crypto import encrypt_secret
from daalu_automation.models import ConfigManagerTenant, Integration, Tenant

_BASE_DOMAIN = "cmtools.daalu.io"


# ── Fakes ───────────────────────────────────────────────────────────────


@dataclass
class _FakeTenant:
    slug: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class _FakeEnv:
    tenant_id: uuid.UUID
    slug: str
    is_primary: bool = False
    gateway_internal_url: str | None = None
    target_cluster_tunnel_id: uuid.UUID | None = None


@dataclass
class _FakeIntegration:
    tenant_id: uuid.UUID
    config: dict
    cluster_tunnel_id: uuid.UUID | None = None


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    """Answers the selects ``_resolve`` issues, by inspecting the statement.

    Dispatching on the mapped entity plus the compiled bind params keeps the
    fake honest: it can only answer a query the production code actually
    builds, so a change in filter shape surfaces here instead of silently
    passing.
    """

    def __init__(self, tenants, envs, integrations) -> None:
        self.tenants = tenants
        self.envs = envs
        self.integrations = integrations

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        params = stmt.compile().params
        if entity is Tenant:
            want = params["slug_1"]
            wanted = want if isinstance(want, (list, tuple)) else [want]
            return _FakeResult([t for t in self.tenants if t.slug in wanted])
        if entity is ConfigManagerTenant:
            rows = [e for e in self.envs if e.tenant_id == params["tenant_id_1"]]
            if "slug_1" in params:
                rows = [e for e in rows if e.slug == params["slug_1"]]
            else:  # the is_primary lookup
                rows = [e for e in rows if e.is_primary]
            return _FakeResult(rows)
        if entity is Integration:
            return _FakeResult(
                [i for i in self.integrations if i.tenant_id == params["tenant_id_1"]]
            )
        raise AssertionError(f"unexpected query against {entity}")


def _integration_config(**extra) -> dict:
    cfg = {
        "config_store_url": "https://svc-config-store.example",
        "render_url": "https://svc-render.example",
        "workflow_url": "https://svc-workflow.example",
        "keycloak_client_id": "daalu-hub-nvcm",
        "keycloak_client_secret_ciphertext": encrypt_secret("s3cret"),
    }
    cfg.update(extra)
    return cfg


@pytest.fixture
def world(monkeypatch):
    """Two tenants: ``acme`` (primary + a ``prod`` env) and ``globex``."""
    acme = _FakeTenant(slug="acme")
    globex = _FakeTenant(slug="globex")
    envs = [
        _FakeEnv(
            tenant_id=acme.id,
            slug="default",
            is_primary=True,
            gateway_internal_url="http://gw-primary.svc",
        ),
        _FakeEnv(
            tenant_id=acme.id,
            slug="prod",
            gateway_internal_url="http://gw-prod.svc",
        ),
        _FakeEnv(
            tenant_id=globex.id,
            slug="default",
            is_primary=True,
            gateway_internal_url="http://gw-globex.svc",
        ),
    ]
    integrations = [
        _FakeIntegration(
            tenant_id=acme.id,
            config=_integration_config(gateway_internal_url="http://gw-integ.svc"),
        ),
        _FakeIntegration(tenant_id=globex.id, config=_integration_config()),
    ]
    db = _FakeDB([acme, globex], envs, integrations)

    @asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(tp, "AsyncSessionLocal", _session)

    async def _no_proxy(_db, _tunnel_id):
        return None

    monkeypatch.setattr(tp, "get_proxy_url", _no_proxy)

    async def _token(**_kw):
        return "machine-token"

    monkeypatch.setattr(tp, "get_machine_token", _token)
    return {
        "acme": acme,
        "globex": globex,
        "envs": envs,
        "integrations": integrations,
        "db": db,
    }


def _claims(tenant) -> dict:
    return {"tid": str(tenant.id)}


# ── Host parsing ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("acme.cmtools.daalu.io", (None, "acme")),
        ("nautobot-acme.cmtools.daalu.io", ("nautobot", "acme")),
        ("prod-acme.cmtools.daalu.io", (None, "prod-acme")),
        ("nautobot-prod-acme.cmtools.daalu.io", ("nautobot", "prod-acme")),
        # Longest tool prefix first, so this is config-store and not a
        # hypothetical `config` tool of an environment called `store`.
        ("config-store-prod-acme.cmtools.daalu.io", ("config-store", "prod-acme")),
        ("elsewhere.example.com", None),
        # Must be a single label — the wildcard cert only covers one.
        ("a.b.cmtools.daalu.io", None),
    ],
)
def test_parse_host(host, expected):
    assert tp._parse_host(host, _BASE_DOMAIN) == expected


# ── Resolution ──────────────────────────────────────────────────────────


async def test_legacy_host_resolves_to_the_primary_environment(world):
    """The pre-environment URL shape keeps working, unchanged."""
    target = await tp._resolve("acme", _claims(world["acme"]))
    assert target.gateway_url == "http://gw-primary.svc"


async def test_env_host_resolves_to_that_environment(world):
    target = await tp._resolve("prod-acme", _claims(world["acme"]))
    assert target.gateway_url == "http://gw-prod.svc"


async def test_other_tenants_host_is_forbidden(world):
    """The ownership check, on both host shapes.

    A globex session must not reach acme's consoles even though it holds a
    perfectly valid hub session — this is the whole boundary.
    """
    for label in ("acme", "prod-acme"):
        with pytest.raises(tp._Forbidden):
            await tp._resolve(label, _claims(world["globex"]))


async def test_no_session_is_unauthorized(world):
    with pytest.raises(tp._Unauthorized):
        await tp._resolve("acme", None)


async def test_unknown_label_is_not_resolvable(world):
    with pytest.raises(tp._ProxyUnavailable):
        await tp._resolve("nope-nobody", _claims(world["acme"]))


async def test_unknown_environment_of_a_known_tenant_does_not_fall_back(world):
    """``staging-acme`` with no such environment must not land on the primary."""
    with pytest.raises(tp._ProxyUnavailable):
        await tp._resolve("staging-acme", _claims(world["acme"]))


async def test_longest_tenant_slug_wins(world, monkeypatch):
    """``prod-b-acme`` is ambiguous; anchor on the longer *tenant* match.

    A tenant slug is globally unique while an environment slug is only unique
    within its tenant, so preferring the longer tenant is the reading that
    cannot hand traffic to a different customer.
    """
    b_acme = _FakeTenant(slug="b-acme")
    world["db"].tenants.append(b_acme)
    world["db"].envs.append(
        _FakeEnv(
            tenant_id=b_acme.id,
            slug="prod",
            gateway_internal_url="http://gw-b-acme-prod.svc",
        )
    )
    # `acme` also has an environment that would match the shorter split, so
    # the two readings genuinely compete.
    world["db"].envs.append(
        _FakeEnv(
            tenant_id=world["acme"].id,
            slug="prod-b",
            gateway_internal_url="http://gw-acme-prod-b.svc",
        )
    )
    world["db"].integrations.append(
        _FakeIntegration(tenant_id=b_acme.id, config=_integration_config())
    )

    target = await tp._resolve("prod-b-acme", _claims(b_acme))
    assert target.gateway_url == "http://gw-b-acme-prod.svc"
    # And the losing reading is genuinely forbidden rather than merely unused.
    with pytest.raises(tp._Forbidden):
        await tp._resolve("prod-b-acme", _claims(world["acme"]))


async def test_whole_label_as_tenant_slug_beats_an_env_split(world):
    """A tenant literally slugged ``prod-acme`` owns that host outright.

    Otherwise creating an environment could hijack an existing tenant's host.
    """
    prod_acme = _FakeTenant(slug="prod-acme")
    world["db"].tenants.append(prod_acme)
    world["db"].envs.append(
        _FakeEnv(
            tenant_id=prod_acme.id,
            slug="default",
            is_primary=True,
            gateway_internal_url="http://gw-tenant-prod-acme.svc",
        )
    )
    world["db"].integrations.append(
        _FakeIntegration(tenant_id=prod_acme.id, config=_integration_config())
    )

    target = await tp._resolve("prod-acme", _claims(prod_acme))
    assert target.gateway_url == "http://gw-tenant-prod-acme.svc"


async def test_secondary_environment_never_falls_back_to_the_primary_gateway(world):
    """A not-yet-provisioned secondary must fail closed, not borrow.

    Falling back to the Integration's ``gateway_internal_url`` would forward
    this environment's traffic — including writes — into the primary's stack.
    """
    prod = next(e for e in world["envs"] if e.slug == "prod")
    prod.gateway_internal_url = None
    with pytest.raises(tp._ProxyUnavailable):
        await tp._resolve("prod-acme", _claims(world["acme"]))


async def test_primary_falls_back_to_the_integration_gateway(world):
    """Rows provisioned before the column existed still resolve."""
    primary = next(
        e for e in world["envs"] if e.tenant_id == world["acme"].id and e.is_primary
    )
    primary.gateway_internal_url = None
    target = await tp._resolve("acme", _claims(world["acme"]))
    assert target.gateway_url == "http://gw-integ.svc"
