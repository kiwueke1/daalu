"""Pytest fixtures for the multi-tenancy test suite.

These tests need a real Postgres because the models use the
postgresql-dialect ``UUID`` type and the SQL paths under test
(unique-hash lookup, FK cascade, JSON column write/read) all behave
differently on other backends. By default we point at a separate
database named ``daalu_automation_test`` on the same Postgres instance
the dev .env uses; override with ``DAALU_TEST_DATABASE_URL`` to point
elsewhere. If the test DB can't be reached the entire suite is skipped
with a helpful message — CI sets up the DB via the standard
`createdb` step.

Two tenants and their users are pre-seeded in every test so cross-tenant
isolation can be exercised without per-test boilerplate. The httpx
AsyncClient is wired through ASGITransport so the same middleware that
runs in prod is exercised here — testing the routers directly would
miss the AuthGateMiddleware and the cookie/Bearer flow.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# DB URL resolution
# ---------------------------------------------------------------------------

_DEFAULT_TEST_DB_URL = (
    "postgresql+asyncpg://daalu:daalu_password@localhost:5433/daalu_automation_test"
)
TEST_DB_URL = os.environ.get("DAALU_TEST_DATABASE_URL", _DEFAULT_TEST_DB_URL)
# Sync URL is needed by Alembic-style schema management; we use create_all
# off the async engine so this is only for show, but keep it consistent.
TEST_DB_SYNC_URL = TEST_DB_URL.replace("+asyncpg", "")

# Set env BEFORE any daalu_automation modules import — config is lru_cached.
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["DATABASE_SYNC_URL"] = TEST_DB_SYNC_URL
# Keep app secret deterministic so JWTs verify across processes.
os.environ.setdefault("SECRET_KEY", "test-secret-do-not-use-in-prod")
# Service tokens are signed with a *separate* key. Without this, any test that
# mints one passes locally (a developer .env supplies it) and fails in CI,
# which has no .env — so set it here rather than per-test.
os.environ.setdefault(
    "SERVICE_TOKEN_SECRET_KEY", "test-service-token-secret-do-not-use-in-prod"
)
# Auth cookie must not be Secure-only in tests (no TLS in ASGI client).
os.environ["AUTH_COOKIE_SECURE"] = "false"
# Disable any platform-wide ingest key — Slice A requires per-tenant keys.
os.environ["INGEST_API_KEY"] = ""


def _can_reach_pg() -> bool:
    """Quick TCP-level reachability check so we skip cleanly when PG is down."""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(TEST_DB_URL.replace("+asyncpg", ""))
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


pytestmark_pg_required = pytest.mark.skipif(
    not _can_reach_pg(),
    reason=(
        f"Postgres at {TEST_DB_URL} is unreachable. "
        "Start it (e.g. docker compose up -d postgres) or set "
        "DAALU_TEST_DATABASE_URL to a reachable test DB."
    ),
)


# ---------------------------------------------------------------------------
# Engine + schema setup
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _engine():
    if not _can_reach_pg():
        pytest.skip(f"Postgres at {TEST_DB_URL} unreachable")
    # Import AFTER env is set so config picks up the test DB URL.
    import daalu_automation.models  # noqa: F401  — register tables

    # Side-effect import — modules register their adapters / agents /
    # workflows / briefings here. Tests that look up integrations by
    # provider slug rely on this.
    import daalu_automation.modules  # noqa: F401
    from daalu_automation.database import Base

    engine = create_async_engine(TEST_DB_URL, future=True, poolclass=None)
    async with engine.begin() as conn:
        # Drop then create so prior failed runs don't leak state.
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def db(_engine) -> AsyncIterator[AsyncSession]:
    """Per-test session that wipes operational tables on entry.

    We don't wrap in a SAVEPOINT — the routers commit, and the FastAPI
    dependency opens its own session anyway. Truncate-before-each-test
    is fast enough for a few hundred tests and avoids cross-test bleed.
    """
    async with _engine.begin() as conn:
        # Cascade so FK-dependent tables clear together. Quote-list keeps
        # this readable; add new tables here as they're introduced.
        await conn.execute(
            text(
                "TRUNCATE TABLE "
                "agent_runs, alerts, briefings, change_proposals, "
                "cluster_tunnels, events, "
                "infra_incidents, infra_services, integrations, "
                "recommendations, "
                "workflow_runs, users, tenants "
                "RESTART IDENTITY CASCADE"
            )
        )
    Session = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session


# ---------------------------------------------------------------------------
# Tenant + user seeding
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def seeded(db: AsyncSession):
    """Two tenants (A, B) each with one user; plus a platform superuser.

    Returned struct exposes ids, cleartext ingest keys (for testing the
    webhook path), and pre-issued JWTs (for testing browser/API paths).
    Tests are encouraged to add additional rows scoped to A or B and
    then assert isolation — never paste hard-coded tenant_ids into the
    test body, fetch them off this fixture.
    """
    from daalu_automation.core.auth import (
        generate_ingest_api_key,
        hash_ingest_api_key,
        hash_password,
        issue_token,
    )
    from daalu_automation.models import Tenant, User

    tenant_a = Tenant(
        name="Tenant A",
        slug="tenant-a",
        ingest_api_key_hash=None,  # set below
    )
    tenant_b = Tenant(
        name="Tenant B",
        slug="tenant-b",
        ingest_api_key_hash=None,
    )
    db.add_all([tenant_a, tenant_b])
    await db.flush()

    key_a = generate_ingest_api_key()
    key_b = generate_ingest_api_key()
    tenant_a.ingest_api_key_hash = hash_ingest_api_key(key_a)
    tenant_b.ingest_api_key_hash = hash_ingest_api_key(key_b)

    user_a = User(
        email="alice@tenant-a.example",
        full_name="Alice in A",
        hashed_password=hash_password("password-a"),
        tenant_id=tenant_a.id,
        is_admin=True,
        is_superuser=False,
    )
    user_b = User(
        email="bob@tenant-b.example",
        full_name="Bob in B",
        hashed_password=hash_password("password-b"),
        tenant_id=tenant_b.id,
        is_admin=True,
        is_superuser=False,
    )
    superuser = User(
        email="root@platform.example",
        full_name="Platform Root",
        hashed_password=hash_password("password-root"),
        tenant_id=tenant_a.id,  # superuser still belongs to *some* tenant
        is_admin=True,
        is_superuser=True,
    )
    db.add_all([user_a, user_b, superuser])
    await db.commit()
    await db.refresh(tenant_a)
    await db.refresh(tenant_b)
    await db.refresh(user_a)
    await db.refresh(user_b)
    await db.refresh(superuser)

    token_a, _ = issue_token(
        user_id=str(user_a.id), tenant_id=str(tenant_a.id), is_admin=True
    )
    token_b, _ = issue_token(
        user_id=str(user_b.id), tenant_id=str(tenant_b.id), is_admin=True
    )
    token_super, _ = issue_token(
        user_id=str(superuser.id), tenant_id=str(tenant_a.id), is_admin=True
    )

    return _Seeded(
        tenant_a_id=tenant_a.id,
        tenant_b_id=tenant_b.id,
        user_a_id=user_a.id,
        user_b_id=user_b.id,
        superuser_id=superuser.id,
        ingest_key_a=key_a,
        ingest_key_b=key_b,
        token_a=token_a,
        token_b=token_b,
        token_super=token_super,
    )


class _Seeded:
    """Plain container for fixture state — tests dot-access these fields."""

    __slots__ = (
        "tenant_a_id",
        "tenant_b_id",
        "user_a_id",
        "user_b_id",
        "superuser_id",
        "ingest_key_a",
        "ingest_key_b",
        "token_a",
        "token_b",
        "token_super",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# HTTP client wired through ASGI
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def client(_engine) -> AsyncIterator[AsyncClient]:
    """httpx AsyncClient → ASGITransport → real FastAPI app.

    The app's lifespan is bypassed via ``raise_app_exceptions=False`` so
    we don't re-create tables on every test — the session-scoped engine
    fixture handles that once. Cookies + Bearer headers behave identically
    to a real browser/curl call.
    """
    from daalu_automation.api.main import create_app

    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=True)
    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as ac:
        yield ac


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def ingest_headers(key: str) -> dict[str, str]:
    return {"X-Daalu-Key": key}


# ---------------------------------------------------------------------------
# SoT / device-management fixtures
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Tiny in-memory Redis stand-in for the event-bus path.

    Only supports the calls publish() / subscribe() actually make:
    xadd, xgroup_create, xreadgroup, xack. The SoT tests only exercise
    publish(), so xadd is all the test path really needs."""

    def __init__(self) -> None:
        self._counter = 0
        self.entries: list[tuple[str, str, dict[str, str]]] = []

    async def xadd(self, stream_key: str, fields: dict[str, str]) -> str:
        self._counter += 1
        sid = f"0-{self._counter}"
        self.entries.append((stream_key, sid, fields))
        return sid

    async def xgroup_create(self, *_a, **_kw) -> None:  # noqa: D401
        return None

    async def xreadgroup(self, *_a, **_kw):  # noqa: D401
        return []

    async def xack(self, *_a, **_kw) -> int:  # noqa: D401
        return 0


@pytest.fixture
def fake_redis(monkeypatch):
    """Replace the lazy Redis client in core.events with an in-memory fake.

    Lets the SoT tests call publish() (which both xadds and persists the
    event to Postgres) without a running Redis broker. Returns the fake
    so tests can inspect the captured stream entries."""
    fake = _FakeRedis()

    async def _get():
        return fake

    monkeypatch.setattr("daalu_automation.core.events._get_redis", _get)
    return fake


@pytest_asyncio.fixture(loop_scope="session")
async def nautobot_integration(db, seeded):
    """Seed a Nautobot Integration row for tenant A with encrypted token + webhook secret.

    Returns the cleartext webhook secret so tests can sign request
    bodies with the same key the route will verify against."""
    from daalu_automation.core.crypto import encrypt_secret
    from daalu_automation.models import Integration, IntegrationStatus

    cleartext_secret = "test-hmac-secret-do-not-use"
    row = Integration(
        tenant_id=seeded.tenant_a_id,
        provider="nautobot",
        module="infra",
        name="Nautobot for A",
        status=IntegrationStatus.connected,
        config={
            "url": "https://nautobot.test",
            "token_ciphertext": encrypt_secret("dummy-token"),
            "webhook_secret_ciphertext": encrypt_secret(cleartext_secret),
            "nautobot_tenant_slug": "tenant-a-in-nautobot",
        },
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return type(
        "_NautobotIntegrationFixture",
        (),
        {
            "integration_id": row.id,
            "webhook_secret": cleartext_secret,
        },
    )


@pytest_asyncio.fixture(loop_scope="session")
async def ssh_credentials_integration(db, seeded):
    """Seed a tenant-wide ssh_credentials Integration row for tenant A.

    The reconciler / executor resolves device credentials by reading
    this row. v1 stores a single private key keyed only to the tenant;
    PR 2 will introduce per-device overrides."""
    from daalu_automation.core.crypto import encrypt_secret
    from daalu_automation.models import Integration, IntegrationStatus

    # Not a real key — adapter test path is monkey-patched so we never
    # actually try to load it through asyncssh.import_private_key.
    private_key_pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"
    row = Integration(
        tenant_id=seeded.tenant_a_id,
        provider="ssh_credentials",
        module="infra",
        name="SSH creds for A",
        status=IntegrationStatus.connected,
        config={
            "user": "daalu",
            "port": 22,
            "sudo": True,
            "private_key_ciphertext": encrypt_secret(private_key_pem),
        },
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
def mock_asyncssh(monkeypatch):
    """Patch asyncssh.connect with an async context manager returning a fake conn.

    Tests call ``mock_asyncssh({"hostname": "web01"})`` to set up canned
    stdout per substring. The fake also avoids private-key parsing
    (the real ``asyncssh.import_private_key`` would reject our dummy
    PEM string)."""

    class _FakeResult:
        def __init__(self, stdout="", exit_status=0, stderr=""):
            self.stdout = stdout
            self.stderr = stderr
            self.exit_status = exit_status

    class _FakeConn:
        def __init__(self, scripted: dict[str, str]):
            self._scripted = scripted
            self.commands: list[str] = []

        async def run(self, cmd: str, **_kw):
            self.commands.append(cmd)
            for needle, out in self._scripted.items():
                if needle in cmd:
                    return _FakeResult(stdout=out, exit_status=0)
            return _FakeResult(stdout="", exit_status=0)

        def close(self):  # noqa: D401
            return None

        async def wait_closed(self):
            return None

    holder: dict[str, _FakeConn | None] = {"conn": None}

    def _install(scripted: dict[str, str]):
        async def _connect(**_kw):
            conn = _FakeConn(scripted)
            holder["conn"] = conn
            return conn

        monkeypatch.setattr("asyncssh.connect", _connect)
        # Avoid the real private-key parser — it rejects our fake PEM.
        monkeypatch.setattr(
            "asyncssh.import_private_key", lambda pem: pem
        )
        return holder

    return _install


@pytest.fixture
def mock_nautobot_transport(monkeypatch):
    """Mount an httpx.MockTransport into NautobotSoT's client factory.

    Tests pass a dict of ``(method, path) -> response_body`` and the
    mock dispatches by request key. The path component is matched
    exactly; query strings are ignored."""
    import httpx

    def _install(routes: dict[tuple[str, str], dict]):
        def handler(req: httpx.Request) -> httpx.Response:
            key = (req.method, req.url.path)
            body = routes.get(key)
            if body is None:
                # Default: empty list-results, so list_devices() sees []
                # rather than a parse error on an unmocked route.
                return httpx.Response(200, json={"results": [], "data": {}})
            return httpx.Response(200, json=body)

        transport = httpx.MockTransport(handler)

        def _build(
            url: str,
            token: str,
            *,
            proxy_url: str | None = None,
            forward_host: str | None = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url=url.rstrip("/"),
                transport=transport,
                headers={"Authorization": f"Token {token}"},
            )

        monkeypatch.setattr(
            "daalu_automation.core.sot.nautobot._build_http_client", _build
        )

    return _install


# ---------------------------------------------------------------------------
# AI-Ops maturity: keep the agent loop hermetic
# ---------------------------------------------------------------------------


@pytest.fixture
def no_aiops_side_calls(monkeypatch):
    """No real LLM or cluster from the AOMM side paths: the runbook drafter
    (runs after a verified fix) and the write preview would otherwise use the
    developer's .env key and ~/.kube/config. Tests that exercise those paths
    themselves don't use this fixture."""
    from daalu_automation.core import llm
    from daalu_automation.core.aiops import preview

    async def no_llm(**_):
        raise llm.LLMUnavailable("disabled in tests")

    async def no_preview(*_, **__):
        return {}

    monkeypatch.setattr(llm, "complete_json", no_llm)
    monkeypatch.setattr(preview, "preview_write", no_preview)
