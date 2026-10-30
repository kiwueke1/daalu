# 59. Test layout

`tests/` houses pytest tests. The layout follows the standard
three-tier model: unit, integration, end-to-end.

---

## Top-level

```
tests/
├── conftest.py              # global fixtures (db, client, factories)
├── unit/                    # fast, isolated; no DB
├── integration/             # full stack with real DB and Redis
├── e2e/                     # against a running deployment
└── factories/               # SQLAlchemy factories (factory_boy)
```

We deliberately don't have a `tests/<module>/` mirroring
`src/daalu_automation/<module>/`. Tests live by their tier;
the module structure is implicit in the test file names.

---

## Conftest

`tests/conftest.py` defines fixtures used everywhere:

```python
@pytest_asyncio.fixture
async def db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine)
    async with SessionLocal() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest_asyncio.fixture
async def client(db):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

@pytest.fixture
def tenant_factory(db):
    return TenantFactory(db)

@pytest.fixture
def user_factory(tenant_factory, db):
    return UserFactory(db, tenant_factory)
```

The `db` fixture uses SQLAlchemy's in-memory SQLite for unit
tests and a real Postgres for integration tests. The switch
is via env var.

---

## Unit tests

```
tests/unit/
├── test_auth.py             # hash_password, generate_pat, issue_token
├── test_llm_router.py       # routing decisions (mocked endpoints)
├── test_reconciler_diff.py  # diff + normalization logic
├── test_tenant_isolation_logic.py  # the helper functions
└── ...
```

Unit tests:

- Don't touch the network.
- Don't touch a real DB. (in-memory SQLite is OK for trivial
  ORM tests; we mock for harder cases.)
- Run in <30s for the whole suite.

---

## Integration tests

```
tests/integration/
├── test_api_alerts.py       # CRUD on alerts via FastAPI client
├── test_api_proposals.py    # full proposal lifecycle
├── test_celery_tasks.py     # real Celery + Redis broker
├── test_federation_join.py  # mock hub, real DB
├── test_tenant_isolation.py # cross-tenant access denied
└── ...
```

Integration tests:

- Use a real Postgres (containerized for CI, configurable for
  local).
- Use a real Redis.
- Don't reach external services (those are mocked).
- Run in ~3 minutes.

The `test_tenant_isolation.py` is the load-bearing one for
the Chapter 57 guarantee. Every PR must pass it.

---

## End-to-end tests

```
tests/e2e/
├── conftest.py              # points at a deployed Daalu URL
├── test_login_logout.py
├── test_first_cloud_account.py
├── test_alert_explanation.py
└── ...
```

E2E tests run against a deployed staging instance. They:

- Hit a real URL.
- Use a dedicated tenant for testing.
- Are tagged with `@pytest.mark.slow` so unit/integration
  runs skip them.
- Run in CI only on tagged release builds.

---

## Factories

```python
# tests/factories/users.py
class UserFactory:
    def __init__(self, db, tenant_factory):
        self.db = db
        self.tenant_factory = tenant_factory

    async def create(self, **kwargs):
        defaults = {
            "email": f"test-{uuid4()}@example.com",
            "password_hash": hash_password("test"),
            "tenant_id": kwargs.pop("tenant_id", None) or (await self.tenant_factory.create()).id,
            "is_admin": False,
        }
        defaults.update(kwargs)
        user = User(**defaults)
        self.db.add(user)
        await self.db.commit()
        return user
```

One factory per model. They take a db and other factories as
inputs to enable the FK graph.

Pattern: use factories for setup, not direct ORM construction
in the test body. Keeps tests focused on what they're
actually testing.

---

## Running tests

```bash
# All
uv run pytest

# By tier
uv run pytest tests/unit
uv run pytest tests/integration
uv run pytest tests/e2e

# Verbose, with coverage, fail fast
uv run pytest -x -v --cov=daalu_automation
```

CI runs unit + integration on every PR; e2e on tagged
releases.

---

## Coverage goals

- Unit: 90 %+ on `core/` (the cross-cutting code).
- Integration: 70 %+ on `api/routers/` (the routes).
- E2E: critical paths covered (login, create proposal,
  approve proposal, tenant create/delete).

We track coverage in CI and fail PRs that drop overall
below 80 %.

---

## What's next

Chapter 60 — integration test patterns.
