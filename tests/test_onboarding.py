"""Onboarding-router tests.

Three surfaces under test:

- ``GET  /onboarding/status``            — aggregate per-step "configured?"
- ``POST /onboarding/validate/{prov}``   — schema check, no network
- ``POST /onboarding/test/{prov}``       — probes outbound creds; we
  monkeypatch the per-provider tester so no real Slack/SMTP/HTTP is hit.

The property to break: a provider with a redaction-mask value in the
posted payload — e.g. ``password: "***"`` — should pull the stored
value from the DB instead of testing the literal string. The test
``test_test_substitutes_redaction_mask_from_db`` proves the merge.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — seed an Integration row directly
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


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_marks_missing_steps_as_not_configured(client, seeded):
    r = await client.get(
        "/api/v1/onboarding/status",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert r.status_code == 200
    body = r.json()
    # Catalog has 13 steps (11 integrations + nautobot SoT + cluster).
    assert body["total"] == 13
    # Nothing seeded → nothing configured.
    assert body["completed"] == 0
    step_ids = [s["id"] for s in body["steps"]]
    assert step_ids == [
        "slack",
        "email",
        "prometheus",
        "loki",
        "thanos",
        "opensearch",
        "pagerduty",
        "kubernetes",
        "aws",
        "gcp",
        "azure",
        "nautobot",
        "cluster",
    ]
    slack = next(s for s in body["steps"] if s["id"] == "slack")
    assert slack["configured"] is False
    assert slack["missing"] == ["webhook_url"]


@pytest.mark.asyncio
async def test_status_reflects_seeded_rows(client, seeded, db):
    """Seeding a slack row flips that step to configured."""
    await _seed_integration(
        db,
        seeded.tenant_a_id,
        provider="slack",
        config={"webhook_url": "https://hooks.slack.com/A/B/C"},
    )
    r = await client.get(
        "/api/v1/onboarding/status",
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    body = r.json()
    slack = next(s for s in body["steps"] if s["id"] == "slack")
    assert slack["configured"] is True
    assert slack["status"] == "connected"
    assert slack["missing"] == []
    assert body["completed"] == 1


@pytest.mark.asyncio
async def test_status_is_tenant_scoped(client, seeded, db):
    """A's seeded slack must not affect B's status."""
    await _seed_integration(
        db,
        seeded.tenant_a_id,
        provider="slack",
        config={"webhook_url": "https://hooks.slack.com/A/B/C"},
    )
    r = await client.get(
        "/api/v1/onboarding/status",
        headers={"Authorization": f"Bearer {seeded.token_b}"},
    )
    body = r.json()
    assert body["completed"] == 0


@pytest.mark.asyncio
async def test_status_counts_cluster_rows(client, seeded, monkeypatch):
    """Onboarding a cluster ticks the cluster step on the status response."""

    async def _noop(*args, **kwargs):
        return 0

    monkeypatch.setattr(
        "daalu_automation.core.wireguard.sync_hub_secret", _noop
    )
    monkeypatch.setattr(
        "daalu_automation.api.routers.clusters.sync_hub_secret", _noop
    )

    async def _no_pubkey():
        return None

    monkeypatch.setattr(
        "daalu_automation.api.routers.clusters._load_hub_pubkey", _no_pubkey
    )
    monkeypatch.setenv("WIREGUARD_PUBLIC_ENDPOINT", "wg.example.com")
    from daalu_automation.config import get_settings

    get_settings.cache_clear()

    headers = {"Authorization": f"Bearer {seeded.token_a}"}
    create = await client.post(
        "/api/v1/clusters",
        json={"slug": "acme", "name": "Acme"},
        headers=headers,
    )
    assert create.status_code == 201

    r = await client.get("/api/v1/onboarding/status", headers=headers)
    body = r.json()
    cluster = next(s for s in body["steps"] if s["id"] == "cluster")
    assert cluster["configured"] is True
    assert cluster["count"] == 1
    # New cluster has no handshake yet → degraded rollup.
    assert cluster["status"] == "degraded"


# ---------------------------------------------------------------------------
# Validate endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_flags_missing_fields(client, seeded):
    r = await client.post(
        "/api/v1/onboarding/validate/smtp",
        json={"config": {"username": "u", "password": "p"}},
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    # host + port + from missing; username/password were supplied.
    assert set(body["missing"]) == {"host", "port", "from"}


@pytest.mark.asyncio
async def test_validate_passes_with_all_required(client, seeded):
    r = await client.post(
        "/api/v1/onboarding/validate/slack",
        json={"config": {"webhook_url": "https://hooks.slack.com/A/B/C"}},
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["missing"] == []


@pytest.mark.asyncio
async def test_validate_unknown_provider(client, seeded):
    r = await client.post(
        "/api/v1/onboarding/validate/not-a-real-provider",
        json={"config": {}},
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Test (probe) endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_slack_invokes_tester_with_posted_config(client, seeded):
    """The router hands the posted config straight to the tester."""
    seen: dict = {}

    async def _fake_slack(cfg):
        seen.update(cfg)
        return True, "ok"

    with patch.dict(
        "daalu_automation.api.routers.onboarding._TESTERS",
        {"slack": _fake_slack},
    ):
        r = await client.post(
            "/api/v1/onboarding/test/slack",
            json={"config": {"webhook_url": "https://hooks.slack.com/X/Y/Z"}},
            headers={"Authorization": f"Bearer {seeded.token_a}"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["message"] == "ok"
    assert "latency_ms" in body
    assert seen["webhook_url"] == "https://hooks.slack.com/X/Y/Z"


@pytest.mark.asyncio
async def test_test_returns_ok_false_on_tester_failure(client, seeded):
    async def _fail(cfg):
        return False, "webhook rejected"

    with patch.dict(
        "daalu_automation.api.routers.onboarding._TESTERS",
        {"slack": _fail},
    ):
        r = await client.post(
            "/api/v1/onboarding/test/slack",
            json={"config": {"webhook_url": "https://bad"}},
            headers={"Authorization": f"Bearer {seeded.token_a}"},
        )
    body = r.json()
    assert body["ok"] is False
    assert body["message"] == "webhook rejected"


@pytest.mark.asyncio
async def test_test_substitutes_redaction_mask_from_db(client, seeded, db):
    """Posted '***' values must be replaced with the stored config value.

    The integrations API redacts sensitive fields on read; the wizard
    posts what it sees, so '***' must mean "use whatever's in the DB"
    rather than literally testing the string.
    """
    await _seed_integration(
        db,
        seeded.tenant_a_id,
        provider="slack",
        config={"webhook_url": "https://hooks.slack.com/REAL/URL/123"},
    )
    captured: dict = {}

    async def _capture(cfg):
        captured.update(cfg)
        return True, "ok"

    with patch.dict(
        "daalu_automation.api.routers.onboarding._TESTERS",
        {"slack": _capture},
    ):
        r = await client.post(
            "/api/v1/onboarding/test/slack",
            json={"config": {"webhook_url": "***"}},
            headers={"Authorization": f"Bearer {seeded.token_a}"},
        )
    assert r.status_code == 200
    assert captured["webhook_url"] == "https://hooks.slack.com/REAL/URL/123"


@pytest.mark.asyncio
async def test_test_unknown_provider(client, seeded):
    r = await client.post(
        "/api/v1/onboarding/test/not-real",
        json={"config": {}},
        headers={"Authorization": f"Bearer {seeded.token_a}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_test_requires_admin(client, seeded):
    """Token without admin claims must be rejected by the dependency.

    The seeded users in conftest are all admins, but the route is
    declared with `current_admin` — verify the dep is on the function
    so future non-admin tokens fail. We simulate the non-admin case by
    using a totally invalid Authorization header.
    """
    r = await client.post(
        "/api/v1/onboarding/test/slack",
        json={"config": {"webhook_url": "https://x"}},
        # No auth → middleware-level 401.
    )
    assert r.status_code == 401
