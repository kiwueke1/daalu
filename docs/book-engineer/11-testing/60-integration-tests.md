# 60. Integration tests

Integration tests are the workhorse — they exercise the full
backend stack against a real Postgres and Redis. This chapter
covers the patterns we use.

---

## What "integration" means here

- Full FastAPI app stood up in-process.
- Real Postgres (containerized).
- Real Redis (containerized).
- HTTP requests via `httpx.AsyncClient`.
- External APIs mocked.

This sits between unit (no IO) and e2e (deployed env).

---

## Anatomy of a test

```python
async def test_user_can_acknowledge_alert(
    client: AsyncClient,
    user_factory: UserFactory,
    alert_factory: AlertFactory,
):
    user = await user_factory.create()
    alert = await alert_factory.create(tenant_id=user.tenant_id)
    token = await login_as(client, user)

    r = await client.post(
        f"/api/v1/alerts/{alert.id}/acknowledge",
        cookies={"daalu_session": token},
    )
    assert r.status_code == 200

    # Verify side effect
    refreshed = await db.get(Alert, alert.id)
    assert refreshed.assignee_id == user.id
```

Conventions:

- Async test functions.
- Fixtures inject everything: db, client, factories.
- `login_as` helper sets up the session cookie.
- Assert the response *and* the side effect.

---

## Mocking external services

For tests that touch external services:

```python
@pytest.fixture
def mock_anthropic(monkeypatch):
    calls = []
    async def fake_complete(**kwargs):
        calls.append(kwargs)
        return InferenceResponse(content="mock response", ...)
    monkeypatch.setattr("daalu_automation.core.llm_router.providers.anthropic.AnthropicClient.complete", fake_complete)
    return calls

async def test_assistant_calls_anthropic(client, mock_anthropic, user):
    r = await client.post("/api/v1/copilot/chat", json={"message": "hi"})
    assert r.status_code == 200
    assert len(mock_anthropic) == 1
```

We use `monkeypatch` over `unittest.mock` because it's
scope-aware (auto-undoes after the test) and reads more
clearly.

For mocking HTTP services (e.g., simulating AWS), we use
`respx` (httpx mock):

```python
@pytest.fixture
def mock_aws(respx_mock):
    respx_mock.post("https://ec2.us-east-1.amazonaws.com/").mock(
        return_value=httpx.Response(200, content=b"<EC2Response>...</EC2Response>")
    )
```

---

## Database isolation

Each test gets a fresh DB. We use either:

- **Transactional rollback** — open a transaction at the
  start of the test, rollback at the end. Fast.
- **Schema recreate** — drop+create tables per test. Slow
  but uncomplicated.

We use transactional rollback by default, falling back to
recreate when a test commits explicitly (rare).

---

## Patterns we like

### Use real time, not mocked

```python
# Don't:
with freezegun.freeze_time("2026-01-01"):
    ...

# Do:
created_at = datetime.now(tz=timezone.utc)
assert alert.created_at <= created_at + timedelta(seconds=5)
```

Frozen time hides race conditions. Real time, with reasonable
windows, catches them.

### Test the API, not the implementation

Prefer:

```python
r = await client.post("/api/v1/alerts/{id}/acknowledge")
assert r.status_code == 200
```

Over:

```python
await acknowledge_alert(alert, user)
```

The former exercises auth, validation, response shape. The
latter is just calling a function — that's a unit test.

### Names that describe behavior

```python
async def test_user_cannot_approve_their_own_proposal(...):
    ...

async def test_pat_with_revoked_at_in_past_is_rejected(...):
    ...
```

Bad:

```python
async def test_approve_1(...): ...
async def test_pat_edge_case(...): ...
```

The test name should fail in human-readable English when
something breaks.

---

## Tenant isolation tests

`tests/integration/test_tenant_isolation.py`:

```python
async def test_user_cannot_read_other_tenant_alert(...):
    tenant_a = await tenant_factory.create()
    tenant_b = await tenant_factory.create()
    user_a = await user_factory.create(tenant_id=tenant_a.id)
    alert_b = await alert_factory.create(tenant_id=tenant_b.id)

    token = await login_as(client, user_a)
    r = await client.get(
        f"/api/v1/alerts/{alert_b.id}",
        cookies={"daalu_session": token},
    )
    assert r.status_code == 404   # NOT 403
```

We have one test like this per route that touches tenant-
scoped resources. The full set is ~40 tests covering every
API endpoint.

If you add a new endpoint, add a tenant-isolation test for
it. CI doesn't enforce this yet but reviewers do.

---

## Running locally

```bash
# Bring up dependencies
docker compose -f dev/docker-compose.yaml up -d postgres redis

# Run integration tests with verbose output
uv run pytest tests/integration -v
```

To run a single test:

```bash
uv run pytest tests/integration/test_tenant_isolation.py::test_user_cannot_read_other_tenant_alert -v
```

---

## What's next

Chapter 61 — end-to-end verification against a live
environment.
