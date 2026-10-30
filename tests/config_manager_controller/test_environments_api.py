"""The controller's named-environment routes.

Create is the interesting one: the slug it derives from the submitted name is
immutable afterwards (it becomes a namespace and a public DNS label), so the
name rules and the per-tenant collision check can only be enforced here, once.

Runs without a database — the route's session is replaced by a fake that
answers the one query it makes and records what would have been inserted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from daalu_automation.config_manager_controller import app as app_mod
from daalu_automation.core.service_tokens import mint_service_token
from daalu_automation.database import get_db
from daalu_automation.models import ConfigManagerTenant, Tenant

_TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_TENANT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


@dataclass
class _FakeTenant:
    id: uuid.UUID
    slug: str = "acme"


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


@dataclass
class _FakeDB:
    """Stands in for the AsyncSession the route depends on."""

    rows: list = field(default_factory=list)
    committed: int = 0
    # The state the *database* holds for a row id, when it has moved on from
    # what the in-memory object still says. That divergence is the whole
    # subject of the staleness tests below; everywhere else the two agree.
    committed_states: dict = field(default_factory=dict)

    def _committed_state(self, row):
        return self.committed_states.get(row.id, row.state)

    async def get(self, model, pk):
        assert model is Tenant
        return _FakeTenant(id=pk) if pk == _TENANT_ID else None

    async def execute(self, stmt):
        desc = stmt.column_descriptions[0]
        assert desc["entity"] is ConfigManagerTenant
        params = stmt.compile().params
        rows = list(self.rows)
        # Apply every predicate present, not just the first: `_find_env_row`
        # filters on both id and tenant_id and means the conjunction.
        if "tenant_id_1" in params:
            rows = [r for r in rows if r.tenant_id == params["tenant_id_1"]]
        if "id_1" in params:
            rows = [r for r in rows if r.id == params["id_1"]]
        if desc["expr"] is not desc["entity"]:
            # A column select — `select(ConfigManagerTenant.state)`, as
            # `_current_state_in_db` makes — yields the value, not the row.
            return _FakeResult([self._committed_state(r) for r in rows])
        return _FakeResult(rows)

    def add(self, row):
        # Mimic the server-side defaults the real insert would apply, so the
        # row is renderable by _to_view immediately after.
        row.id = row.id or uuid.uuid4()
        row.urls = row.urls or {}
        row.last_error = None
        self.rows.append(row)

    async def delete(self, row):
        self.rows = [r for r in self.rows if r is not row]

    def expunge(self, row):
        self.rows = [r for r in self.rows if r is not row]

    async def commit(self):
        self.committed += 1

    async def refresh(self, row):
        row.state = self._committed_state(row)


@pytest.fixture
def client(monkeypatch):
    db = _FakeDB()
    app = app_mod.create_app()
    app.dependency_overrides[get_db] = lambda: db

    # Tunnel selection is a separate concern with its own DB queries; the
    # None result is the documented "no connected tunnel" case.
    async def _no_tunnel(_db, _tenant_id, _explicit):
        return None

    monkeypatch.setattr(app_mod, "_resolve_target_tunnel", _no_tunnel)

    # `with` so FastAPI's startup runs; the reconcile loop it starts is
    # cancelled again on exit and never gets a tick in between.
    with TestClient(app) as c:
        c.db = db
        yield c


def _auth(tenant_id: uuid.UUID = _TENANT_ID) -> dict[str, str]:
    token = mint_service_token(
        tenant_id=str(tenant_id),
        user_id=str(tenant_id),
        purpose="config-manager-provision",
        ttl_seconds=300,
    )
    return {"Authorization": f"Bearer {token}"}


def _create(client, name: str):
    return client.post(
        f"/tenants/{_TENANT_ID}/environments",
        json={"name": name},
        headers=_auth(),
    )


# ── Create ──────────────────────────────────────────────────────────────


def test_first_environment_is_primary_and_later_ones_are_not(client):
    """Primary is what every pre-environment caller resolves.

    The first environment therefore has to claim it — otherwise a tenant
    onboarded through this route would be invisible to the tool proxy, the SoT
    client and the hub's Integration writer. Later ones are purely additive.
    """
    first = _create(client, "Production")
    assert first.status_code == 200, first.text
    assert first.json()["is_primary"] is True
    assert first.json()["slug"] == "production"

    second = _create(client, "Staging")
    assert second.status_code == 200, second.text
    assert second.json()["is_primary"] is False
    assert second.json()["slug"] == "staging"


def test_namespace_and_hostname_are_derived_from_both_slugs(client):
    body = _create(client, "Production").json()
    assert body["namespace"] == "cm-acme-production"
    assert body["base_hostname"].startswith("production-acme.")
    # One label before the base domain keeps the wildcard cert/DNS valid.
    assert body["base_hostname"].split(".", 1)[0].count(".") == 0


def test_duplicate_slug_for_the_same_tenant_is_a_conflict(client):
    assert _create(client, "Production").status_code == 200
    # A different display name that slugifies the same way still collides —
    # the slug is the identity, since it is what becomes the namespace.
    dup = _create(client, "  production  ")
    assert dup.status_code == 409
    assert "already exists" in dup.json()["detail"]


def test_the_same_slug_is_fine_for_a_different_tenant(client):
    assert _create(client, "Production").status_code == 200
    # Uniqueness is per tenant: hostnames carry the tenant slug too.
    client.db.rows.append(
        ConfigManagerTenant(
            tenant_id=_OTHER_TENANT_ID, slug="production", is_primary=True
        )
    )
    assert len([r for r in client.db.rows if r.slug == "production"]) == 2


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("", "empty"),
        ("Render Prod", "wrong service"),  # tool-prefix collision
        ("gateway", "reserved"),
        ("x" * 40, "too long"),
    ],
)
def test_unusable_names_are_rejected_with_a_readable_reason(client, name, reason):
    """400, with the message the hub relays verbatim to whoever typed it."""
    resp = _create(client, name)
    assert resp.status_code == 400, resp.text
    assert reason in resp.json()["detail"]


# ── List / auth ─────────────────────────────────────────────────────────


def test_list_returns_the_tenants_environments(client):
    _create(client, "Production")
    _create(client, "Staging")
    resp = client.get(f"/tenants/{_TENANT_ID}/environments", headers=_auth())
    assert resp.status_code == 200
    assert {e["slug"] for e in resp.json()} == {"production", "staging"}


def test_a_token_for_another_tenant_is_rejected(client):
    """Same check as every other route: claims must match the path tenant."""
    for method, path in (
        ("get", f"/tenants/{_TENANT_ID}/environments"),
        ("post", f"/tenants/{_TENANT_ID}/environments"),
        ("get", f"/tenants/{_TENANT_ID}/environments/{uuid.uuid4()}"),
        ("delete", f"/tenants/{_TENANT_ID}/environments/{uuid.uuid4()}"),
    ):
        kwargs = {"headers": _auth(_OTHER_TENANT_ID)}
        if method == "post":
            kwargs["json"] = {"name": "prod"}
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code == 403, (path, resp.text)


def test_unauthenticated_requests_are_rejected(client):
    resp = client.get(f"/tenants/{_TENANT_ID}/environments")
    assert resp.status_code == 401


# ── Per-environment lookup ──────────────────────────────────────────────


def test_environment_is_scoped_to_its_tenant(client):
    """A valid id for another tenant's environment must not resolve.

    The tenant predicate in ``_find_env_row`` is what stops a caller holding a
    good token for their own tenant from addressing someone else's row by id.
    """
    foreign = ConfigManagerTenant(
        id=uuid.uuid4(),
        tenant_id=_OTHER_TENANT_ID,
        slug="production",
        namespace="cm-other-production",
        base_hostname="production-other.example",
    )
    client.db.rows.append(foreign)
    resp = client.get(
        f"/tenants/{_TENANT_ID}/environments/{foreign.id}", headers=_auth()
    )
    assert resp.status_code == 404


def test_delete_schedules_teardown(client):
    env_id = _create(client, "Staging").json()["id"]
    resp = client.delete(
        f"/tenants/{_TENANT_ID}/environments/{env_id}", headers=_auth()
    )
    assert resp.status_code == 200
    row = next(r for r in client.db.rows if str(r.id) == env_id)
    assert row.state is app_mod.ConfigManagerTenantState.deleting


def test_force_delete_removes_the_row_immediately(client):
    """The escape hatch for a teardown that can never succeed.

    Without it, an environment whose host cluster is gone sits in ``deleting``
    forever, because the reconciler can only clear it by uninstalling from a
    cluster that isn't there.
    """
    env_id = _create(client, "Staging").json()["id"]
    resp = client.delete(
        f"/tenants/{_TENANT_ID}/environments/{env_id}?force=true",
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert not [r for r in client.db.rows if str(r.id) == env_id]


# ── Teardown reconciliation ─────────────────────────────────────────────
#
# "Deleted" has to mean *gone*: a row kept as a tombstone is indistinguishable
# from a live environment to the UI (it lists it), to create (the slug is still
# taken) and to the singular routes (`_find_row` hands the primary back).


@pytest.mark.asyncio
async def test_successful_teardown_deletes_the_row(monkeypatch):
    async def _ok(_row, _kubeconfig, _settings):
        return True, ""

    monkeypatch.setattr(app_mod, "_teardown", _ok)
    db = _FakeDB()
    row = ConfigManagerTenant(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        slug="staging",
        namespace="cm-acme-staging",
        base_hostname="staging-acme.example",
        state=app_mod.ConfigManagerTenantState.deleting,
    )
    db.rows.append(row)

    await app_mod._reconcile_one(db, row)

    assert db.rows == []


@pytest.mark.asyncio
async def test_failed_teardown_keeps_the_row_for_retry(monkeypatch):
    async def _fail(_row, _kubeconfig, _settings):
        return False, "cluster unreachable"

    monkeypatch.setattr(app_mod, "_teardown", _fail)
    db = _FakeDB()
    row = ConfigManagerTenant(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        slug="staging",
        namespace="cm-acme-staging",
        base_hostname="staging-acme.example",
        state=app_mod.ConfigManagerTenantState.deleting,
    )
    db.rows.append(row)

    await app_mod._reconcile_one(db, row)

    assert db.rows == [row]
    assert row.state is app_mod.ConfigManagerTenantState.deleting
    assert row.last_error == "cluster unreachable"


@pytest.mark.asyncio
async def test_destroyed_tombstone_is_swept(monkeypatch):
    """Rows parked in ``destroyed`` by an older build are cleaned up.

    They are what the user sees as a ghost tile: an environment reported
    "destroyed" that never goes away.
    """
    db = _FakeDB()
    row = ConfigManagerTenant(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        slug="production",
        namespace="cm-acme-production",
        base_hostname="production-acme.example",
        state=app_mod.ConfigManagerTenantState.destroyed,
    )
    db.rows.append(row)

    await app_mod._reconcile_one(db, row)

    assert db.rows == []


# ── Staleness: a delete scheduled mid-tick ──────────────────────────────
#
# The reconcile loop is sequential and a provision blocks it for a full
# multi-minute Deployer pass, so by the time a row's turn comes its in-memory
# state can be minutes old. Acting on that copy writes it back — which is how
# a scheduled delete was silently lost in production: DELETE set `deleting`,
# the loop reached the row still believing it was `error`, stamped
# `provisioning` over it, and reinstalled the environment the user had just
# asked to remove. It never deleted, and cycled error→provisioning→error.


async def _always_ready(_kubeconfig):
    return True, []


def _row(state, **kw):
    return ConfigManagerTenant(
        id=kw.pop("id", uuid.uuid4()),
        tenant_id=_TENANT_ID,
        slug=kw.pop("slug", "production"),
        namespace="cm-acme-production",
        base_hostname="production-acme.example",
        state=state,
        **kw,
    )


@pytest.mark.asyncio
async def test_a_delete_scheduled_mid_tick_beats_the_stale_row(monkeypatch):
    """The regression: `deleting` in the DB must win over a stale `error`."""
    torn_down = []
    provisioned = []

    async def _teardown(row, _kubeconfig, _settings):
        torn_down.append(row)
        return True, ""

    async def _provision(row, _kubeconfig, _settings):
        provisioned.append(row)
        return True, ""

    monkeypatch.setattr(app_mod, "_teardown", _teardown)
    monkeypatch.setattr(app_mod, "_provision", _provision)

    db = _FakeDB()
    row = _row(app_mod.ConfigManagerTenantState.error)
    db.rows.append(row)
    # What the loop is holding is what it read at the top of the tick; the
    # DELETE that landed since is only visible in the database.
    db.committed_states[row.id] = app_mod.ConfigManagerTenantState.deleting

    await app_mod._reconcile_one(db, row)

    assert torn_down == [row]
    assert provisioned == []
    assert db.rows == []


@pytest.mark.asyncio
async def test_a_row_deleted_mid_tick_is_not_resurrected(monkeypatch):
    """A force-delete drops the record outright; the stale copy must not act."""
    provisioned = []

    async def _provision(row, _kubeconfig, _settings):
        provisioned.append(row)
        return True, ""

    monkeypatch.setattr(app_mod, "_provision", _provision)

    db = _FakeDB()
    row = _row(app_mod.ConfigManagerTenantState.error)
    # Present to the stale in-memory copy, gone from the database.
    db.committed_states[row.id] = None

    await app_mod._reconcile_one(db, row)

    assert provisioned == []
    assert db.rows == []


@pytest.mark.asyncio
async def test_teardowns_are_reconciled_before_provisions():
    """A delete must not wait out a tick of doomed installs to be noticed."""
    deleting = _row(app_mod.ConfigManagerTenantState.deleting, slug="production")
    erroring = _row(app_mod.ConfigManagerTenantState.error, slug="staging")
    active = _row(app_mod.ConfigManagerTenantState.active, slug="lab")

    ordered = app_mod._teardown_first([erroring, active, deleting])

    assert ordered[0] is deleting
    # Everything else keeps its relative order — this reprioritises teardowns,
    # it does not reshuffle the rest.
    assert ordered[1:] == [erroring, active]


# ── Backoff: a permanently failing install must not eat the loop ────────


@pytest.mark.asyncio
async def test_a_failed_provision_backs_off_instead_of_retrying_every_tick(
    monkeypatch,
):
    """Each retry is a full Deployer pass; retried every 30 s it starves the
    loop, and a teardown queued behind it never gets a turn."""
    attempts = []

    async def _provision(row, _kubeconfig, _settings):
        attempts.append(row)
        return False, "0/1 nodes are available: 1 Too many pods"

    monkeypatch.setattr(app_mod, "_provision", _provision)
    monkeypatch.setattr(
        app_mod.prechecks, "host_cluster_ready", _always_ready
    )

    db = _FakeDB()
    row = _row(app_mod.ConfigManagerTenantState.pending)
    db.rows.append(row)
    app_mod._clear_provision_backoff(row)

    await app_mod._reconcile_one(db, row)
    assert row.state is app_mod.ConfigManagerTenantState.error
    assert len(attempts) == 1

    # The very next tick must leave it alone.
    db.committed_states[row.id] = app_mod.ConfigManagerTenantState.error
    await app_mod._reconcile_one(db, row)
    assert len(attempts) == 1

    # …and once the backoff expires, it tries again.
    app_mod._provision_backoff[row.id] = (1, 0.0)
    await app_mod._reconcile_one(db, row)
    assert len(attempts) == 2

    app_mod._clear_provision_backoff(row)


@pytest.mark.asyncio
async def test_backoff_does_not_delay_a_teardown(monkeypatch):
    """The backoff gates provisioning only — a delete is always actioned."""
    async def _teardown(_row, _kubeconfig, _settings):
        return True, ""

    monkeypatch.setattr(app_mod, "_teardown", _teardown)

    db = _FakeDB()
    row = _row(app_mod.ConfigManagerTenantState.deleting)
    db.rows.append(row)
    # As if it had just failed a provision.
    app_mod._provision_backoff[row.id] = (5, __import__("time").monotonic() + 1800)

    await app_mod._reconcile_one(db, row)

    assert db.rows == []
    assert row.id not in app_mod._provision_backoff
