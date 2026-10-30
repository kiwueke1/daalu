"""Per-tenant integration isolation — Slice B of the multi-tenancy rollout.

These tests prove the critical correctness property: a notification or
adapter call for tenant A never touches tenant B's credentials. They
mock the outbound HTTP layer (httpx for Slack/Prometheus) and the SMTP
transport so no real network is needed, then inspect what the code
*would* have sent.

The property to break would be a regression where a missing integration
row silently falls through to ``Settings`` env defaults that happen to
hold tenant B's credentials — that's how cross-tenant leaks happen in
practice in this kind of system. Tests verify the no-fall-through rule:
no row + no env default = no send.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Slack — per-tenant webhook URL
# ---------------------------------------------------------------------------


async def _seed_integration(db, tenant_id, *, provider: str, config: dict):
    from daalu_automation.models import Integration, IntegrationStatus

    row = Integration(
        tenant_id=tenant_id,
        provider=provider,
        module="infra",
        name=f"{provider} for {tenant_id}",
        status=IntegrationStatus.connected,
        config=config,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_send_slack_uses_tenant_a_webhook(db, seeded):
    """send_slack(tenant_id=A) hits A's webhook URL — never B's."""
    from daalu_automation.core.notify import send_slack

    await _seed_integration(
        db,
        seeded.tenant_a_id,
        provider="slack",
        config={"webhook_url": "https://hooks.slack.com/A/A/A"},
    )
    await _seed_integration(
        db,
        seeded.tenant_b_id,
        provider="slack",
        config={"webhook_url": "https://hooks.slack.com/B/B/B"},
    )

    posts = []

    class _MockResp:
        def raise_for_status(self):
            return None

    async def _fake_post(self, url, json=None, **kw):
        posts.append((url, json))
        return _MockResp()

    with patch("httpx.AsyncClient.post", new=_fake_post):
        ok = await send_slack("hello A", tenant_id=seeded.tenant_a_id, db=db)
        assert ok is True

    assert len(posts) == 1
    url, body = posts[0]
    assert url == "https://hooks.slack.com/A/A/A"
    assert body["text"] == "hello A"
    # The B URL must never appear when sending for tenant A.
    assert "B/B/B" not in url


@pytest.mark.asyncio
async def test_send_slack_no_row_no_env_returns_false(db, seeded, monkeypatch):
    """Missing slack integration + empty env default → False, no send.

    This is the load-bearing correctness invariant: notify must not fall
    through to another tenant's webhook just because the caller's
    integration row is absent.
    """
    from daalu_automation.config import get_settings
    from daalu_automation.core.notify import send_slack

    # Defensive — ensure no env default exists.
    monkeypatch.setattr(get_settings(), "slack_webhook_url", "")

    posts = []

    async def _fake_post(self, url, json=None, **kw):
        posts.append((url, json))
        return MagicMock(raise_for_status=lambda: None)

    with patch("httpx.AsyncClient.post", new=_fake_post):
        ok = await send_slack("nope", tenant_id=seeded.tenant_a_id, db=db)

    assert ok is False
    assert posts == []


@pytest.mark.asyncio
async def test_send_slack_a_does_not_use_b_row(db, seeded):
    """Tenant A having no row + tenant B having one → A still gets no send.

    The critical anti-leak: notify must scope its row lookup to the
    requested tenant, not pick "the first available slack integration".
    """
    from daalu_automation.config import get_settings
    from daalu_automation.core.notify import send_slack

    # B has slack wired; A does not.
    await _seed_integration(
        db,
        seeded.tenant_b_id,
        provider="slack",
        config={"webhook_url": "https://hooks.slack.com/B/B/B"},
    )

    # Belt-and-braces: zero env fallback.
    settings = get_settings()
    original = settings.slack_webhook_url
    settings.slack_webhook_url = ""
    try:
        posts = []

        async def _fake_post(self, url, json=None, **kw):
            posts.append(url)
            return MagicMock(raise_for_status=lambda: None)

        with patch("httpx.AsyncClient.post", new=_fake_post):
            ok = await send_slack("for A", tenant_id=seeded.tenant_a_id, db=db)

        assert ok is False, "A had no row — must not fall through to B's URL"
        assert posts == [], f"Should not have posted; got {posts}"
    finally:
        settings.slack_webhook_url = original


# ---------------------------------------------------------------------------
# Email — per-tenant SMTP relay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_email_uses_tenant_a_smtp(db, seeded):
    """send_email(tenant_id=A) connects to A's SMTP host — not B's."""
    from daalu_automation.core.notify import send_email

    await _seed_integration(
        db,
        seeded.tenant_a_id,
        provider="smtp",
        config={
            "host": "smtp.a.example",
            "port": 587,
            "from": "ops-a@a.example",
            "username": "user-a",
            "password": "pass-a",
        },
    )
    await _seed_integration(
        db,
        seeded.tenant_b_id,
        provider="smtp",
        config={
            "host": "smtp.b.example",
            "port": 587,
            "from": "ops-b@b.example",
            "username": "user-b",
            "password": "pass-b",
        },
    )

    smtp_calls: list[str] = []

    class _MockSMTP:
        def __init__(self, host, port):
            smtp_calls.append(f"connect:{host}:{port}")
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            smtp_calls.append("starttls")

        def login(self, u, p):
            smtp_calls.append(f"login:{u}")

        def send_message(self, msg):
            smtp_calls.append(f"send:{msg['To']}:{msg['From']}")

    with patch("smtplib.SMTP", _MockSMTP):
        ok = await send_email(
            to="oncall@a.example",
            subject="hi",
            body_markdown="body",
            tenant_id=seeded.tenant_a_id,
            db=db,
        )

    assert ok is True
    assert "connect:smtp.a.example:587" in smtp_calls
    assert "login:user-a" in smtp_calls
    # send_message recorded the From — must be A's, never B's.
    assert any(c.startswith("send:") and "ops-a@a.example" in c for c in smtp_calls)
    assert not any("smtp.b.example" in c for c in smtp_calls)
    assert not any("ops-b@b.example" in c for c in smtp_calls)


@pytest.mark.asyncio
async def test_send_email_no_row_no_env_returns_false(db, seeded, monkeypatch):
    from daalu_automation.config import get_settings
    from daalu_automation.core.notify import send_email

    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "")

    smtp_calls = []

    class _MockSMTP:
        def __init__(self, *a, **kw):
            smtp_calls.append("connect")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            ...

        def login(self, *a):
            ...

        def send_message(self, *a):
            ...

    with patch("smtplib.SMTP", _MockSMTP):
        ok = await send_email(
            to="x@example",
            subject="x",
            body_markdown="x",
            tenant_id=seeded.tenant_a_id,
            db=db,
        )
    assert ok is False
    assert smtp_calls == []


# ---------------------------------------------------------------------------
# Prometheus adapter — per-tenant URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prometheus_adapter_uses_tenant_a_url(db, seeded):
    """The Prometheus adapter, invoked for tenant A, hits A's URL.

    Confirms that the per-tenant ingest flow goes through
    integrations.config and not through a global env var.
    """
    from daalu_automation.core.integrations import get_integration

    await _seed_integration(
        db,
        seeded.tenant_a_id,
        provider="prometheus",
        config={"url": "https://alertmanager.a.example"},
    )
    await _seed_integration(
        db,
        seeded.tenant_b_id,
        provider="prometheus",
        config={"url": "https://alertmanager.b.example"},
    )

    captured: list[str] = []

    class _MockResp:
        def __init__(self):
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return []

    async def _fake_get(self, url, **kw):
        captured.append(url)
        return _MockResp()

    adapter = get_integration("prometheus")
    with patch("httpx.AsyncClient.get", new=_fake_get):
        emitted = await adapter.ingest(seeded.tenant_a_id)

    assert emitted == 0  # no alerts in the fake response
    assert len(captured) == 1
    assert captured[0].startswith("https://alertmanager.a.example")
    assert "alertmanager.b.example" not in captured[0]


@pytest.mark.asyncio
async def test_prometheus_adapter_no_url_returns_zero(db, seeded, monkeypatch):
    """No row + no env default → adapter does zero work (no fallthrough)."""
    from daalu_automation.config import get_settings
    from daalu_automation.core.integrations import get_integration

    monkeypatch.setattr(get_settings(), "prometheus_url", "")
    adapter = get_integration("prometheus")
    captured = []

    async def _fake_get(self, url, **kw):
        captured.append(url)
        raise AssertionError("should not have made an HTTP call")

    with patch("httpx.AsyncClient.get", new=_fake_get):
        emitted = await adapter.ingest(seeded.tenant_a_id)
    assert emitted == 0
    assert captured == []


# ---------------------------------------------------------------------------
# Briefing isolation — A's briefing summarises only A's events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_briefing_includes_only_own_tenant_events(db, seeded):
    """An infra briefing generated for tenant A pulls only A's events.

    Briefing.source_event_ids is the audit trail of what the LLM saw;
    if tenant B's event_id shows up there, the LLM context leaked.
    """
    from daalu_automation.models import Event, EventSeverity

    now = datetime.now(tz=timezone.utc)
    a_event = Event(
        tenant_id=seeded.tenant_a_id,
        type="infra.alert.fired",
        module="infra",
        source="test",
        severity=EventSeverity.warning,
        summary="A-event-for-briefing",
        occurred_at=now,
        payload={},
    )
    b_event = Event(
        tenant_id=seeded.tenant_b_id,
        type="infra.alert.fired",
        module="infra",
        source="test",
        severity=EventSeverity.warning,
        summary="B-event-for-briefing",
        occurred_at=now,
        payload={},
    )
    db.add_all([a_event, b_event])
    await db.commit()
    await db.refresh(a_event)
    await db.refresh(b_event)

    from daalu_automation.modules.infra.briefing import InfraBriefingGenerator

    briefing = await InfraBriefingGenerator().generate(tenant_id=seeded.tenant_a_id)

    assert briefing.tenant_id == seeded.tenant_a_id
    src_ids = briefing.source_event_ids or []
    assert str(a_event.id) in src_ids
    assert str(b_event.id) not in src_ids, (
        "tenant B's event leaked into tenant A's briefing"
    )


# ---------------------------------------------------------------------------
# Incident workflow — tenant_id required when creating a new incident
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_workflow_requires_tenant_for_new_incident(db, seeded):
    """coordinate_incident must not auto-default a new incident's tenant.

    The earlier code path created Incident() with no tenant_id, which
    fell back to DEFAULT_TENANT_ID — a silent leak: an alert that
    triggered a workflow without a tenant payload would post to the
    bootstrap tenant's Slack channel.
    """
    from daalu_automation.core.workflows import run_workflow
    from daalu_automation.models import WorkflowRun, WorkflowRunStatus

    run_id = None
    try:
        run_id = await run_workflow(
            "infra.incident.coordinate",
            {"summary": "Synthetic test", "description": "no tenant on payload"},
            tenant_id=seeded.tenant_a_id,
        )
    except ValueError:
        # The workflow itself can raise; the wrapper records the failure
        # on WorkflowRun. Either pathway is acceptable as long as the
        # incident wasn't created against the wrong tenant.
        pass

    if run_id is not None:
        from sqlalchemy import select

        run = (
            await db.execute(
                select(WorkflowRun).where(WorkflowRun.id == run_id)
            )
        ).scalar_one()
        assert run.status == WorkflowRunStatus.failed
        assert "tenant_id" in (run.error_message or "")


# ---------------------------------------------------------------------------
# Adapter on-demand ingest — scoped to caller's tenant via /integrations
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Integration-config CRUD — per-tenant API replacing .env onboarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_crud_creates_then_redacts_on_read(client, seeded):
    """PUT /integrations/config/slack creates; GET redacts the webhook URL."""
    r = await client.put(
        "/api/v1/integrations/config/slack",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
        json={"config": {"webhook_url": "https://hooks.slack.com/A/A/A",
                         "briefing_channel": "#ops-a"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # On create the value is redacted in the response — operator should
    # keep the cleartext they just sent.
    assert body["config"]["webhook_url"] == "***"
    assert body["config"]["briefing_channel"] == "#ops-a"

    list_r = await client.get(
        "/api/v1/integrations/config",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    items = list_r.json()
    assert len(items) == 1
    assert items[0]["provider"] == "slack"
    assert items[0]["config"]["webhook_url"] == "***"


@pytest.mark.asyncio
async def test_config_crud_isolated_by_tenant(client, seeded):
    """A's integration-config rows aren't visible to B."""
    await client.put(
        "/api/v1/integrations/config/slack",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
        json={"config": {"webhook_url": "https://hooks.slack.com/A/A/A"}},
    )
    r_b = await client.get(
        "/api/v1/integrations/config",
        headers={"Authorization": f"Bearer {seeded.token_b}"},
    )
    assert r_b.json() == []


@pytest.mark.asyncio
async def test_config_crud_drives_notify(client, db, seeded):
    """Configuring slack via the API makes send_slack work for that tenant.

    Round-trip: API PUT writes the row, notify reads it back, the request
    target is the URL we PUT in.
    """
    await client.put(
        "/api/v1/integrations/config/slack",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
        json={"config": {"webhook_url": "https://hooks.slack.com/A/from-api"}},
    )

    posts: list[str] = []

    async def _fake_post(self, url, json=None, **kw):
        posts.append(url)
        return MagicMock(raise_for_status=lambda: None)

    from daalu_automation.core.notify import send_slack

    with patch("httpx.AsyncClient.post", new=_fake_post):
        ok = await send_slack("ping", tenant_id=seeded.tenant_a_id, db=db)
    assert ok is True
    assert posts == ["https://hooks.slack.com/A/from-api"]


@pytest.mark.asyncio
async def test_config_crud_delete(client, seeded):
    await client.put(
        "/api/v1/integrations/config/slack",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
        json={"config": {"webhook_url": "https://x"}},
    )
    d = await client.delete(
        "/api/v1/integrations/config/slack",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert d.status_code == 204

    listed = await client.get(
        "/api/v1/integrations/config",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert listed.json() == []


@pytest.mark.asyncio
async def test_config_crud_accepts_kubernetes_provider(client, seeded):
    """PUT /integrations/config/kubernetes stores the row and redacts the
    kubeconfig blob on read — it carries a bearer token."""
    kubeconfig_yaml = (
        "apiVersion: v1\nkind: Config\ncurrent-context: t\n"
        "clusters: [{name: t, cluster: {server: https://example}}]\n"
        "users: [{name: u, user: {token: SECRET}}]\n"
        "contexts: [{name: t, context: {cluster: t, user: u}}]\n"
    )
    r = await client.put(
        "/api/v1/integrations/config/kubernetes",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
        json={"name": "Customer cluster",
              "config": {"kubeconfig": kubeconfig_yaml,
                         "default_context": "t"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "kubernetes"
    assert body["config"]["kubeconfig"] == "***"
    assert body["config"]["default_context"] == "t"

    list_r = await client.get(
        "/api/v1/integrations/config",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    items = [i for i in list_r.json() if i["provider"] == "kubernetes"]
    assert len(items) == 1
    assert items[0]["config"]["kubeconfig"] == "***"


@pytest.mark.asyncio
async def test_synthetic_ingest_stamps_caller_tenant(client, db, seeded):
    """POST /api/v1/integrations/synthetic-infra/ingest emits events for the caller."""
    r = await client.post(
        "/api/v1/integrations/synthetic-infra/ingest",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["events_emitted"] >= 1

    # The emitted event should only be visible to A.
    list_a = await client.get(
        "/api/v1/events", headers={"Authorization": f"Bearer {seeded.token_a}"}
    )
    list_b = await client.get(
        "/api/v1/events", headers={"Authorization": f"Bearer {seeded.token_b}"}
    )
    assert len(list_a.json()) >= 1
    # B should see none of A's synthetic events. (B might see other
    # events from prior tests in the same session, but the truncate
    # fixture nukes them on entry — should be empty here.)
    assert list_b.json() == []


@pytest.mark.asyncio
async def test_ssh_credentials_sealed_on_write_and_resolvable(client, db, seeded):
    """A UI-saved plaintext SSH password is encrypted into the ciphertext
    shape the credential resolver reads — and never echoed back on GET."""
    r = await client.put(
        "/api/v1/integrations/config/ssh_credentials",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
        json={
            "name": "Tenant-wide SSH credentials",
            "config": {"user": "cumulus", "port": 22, "sudo": True,
                       "password": "cumulus"},
        },
    )
    assert r.status_code == 200, r.text

    # Stored shape: ciphertext only — no plaintext secret at rest.
    from sqlalchemy import select as _select

    from daalu_automation.models import Integration as _Integration

    row = (
        await db.execute(
            _select(_Integration).where(
                _Integration.tenant_id == seeded.tenant_a_id,
                _Integration.provider == "ssh_credentials",
            )
        )
    ).scalar_one()
    assert "password" not in row.config
    assert row.config.get("password_ciphertext")

    # ...and the resolver can actually use it.
    from daalu_automation.core import change_proposals as cps
    from daalu_automation.core.sot.models import Device as _Device

    creds = await cps.resolve_credentials(
        db,
        seeded.tenant_a_id,
        _Device(id="d1", name="h1", primary_ip="10.0.0.9", platform="linux",
                transport="linux_ssh", tags=[], extra={}),
    )
    assert creds.user == "cumulus"
    assert creds.password == "cumulus"

    # GET redacts both shapes.
    list_r = await client.get(
        "/api/v1/integrations/config",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    cfg = [i for i in list_r.json() if i["provider"] == "ssh_credentials"][0]["config"]
    assert cfg.get("password_ciphertext") == "***"
    assert cfg.get("password") in (None, "***")
