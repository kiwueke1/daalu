"""Coverage for hosted-Nautobot provisioning (PR 8).

Pins three things that matter:

1. The Nautobot REST round-trip — find-or-create Tenant, find-or-create
   ObjectPermission, mint APIToken — and the per-tenant constraint
   landing on the ObjectPermission *exactly*. A bug here is a
   cross-tenant data leak.
2. The HTTP route writes the resulting URL + token (encrypted) into
   the caller's Integration(provider="nautobot") row, idempotently
   (re-calling upserts rather than duplicating).
3. The hosted-status endpoint correctly gates on the
   managed_nautobot_* settings being present.

The BYO path (operator types url + token into the wizard) is
exercised by the existing test_onboarding suite — only the new
catalog entry needs verifying there, which is covered by the
existing status-endpoint test bumped from 13 → 14.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from tests.conftest import auth_headers

# ---------------------------------------------------------------------------
# Helpers — canned Nautobot REST responses + transport mocker
# ---------------------------------------------------------------------------


def _user_lookup(username: str = "daalu-platform", uid: str = "user-uuid-1") -> dict:
    return {"results": [{"id": uid, "username": username}]}


def _empty_results() -> dict:
    return {"results": []}


def _created_tenant(slug: str, tid: str = "tenant-uuid-1") -> dict:
    # Nautobot 2.x removed the writable `slug` field from Tenant —
    # only `name` is required. Keeping `slug` in the fixture body is
    # harmless (Nautobot ignores unknown fields on response shaping)
    # but it should never be relied on by code under test.
    return {"id": tid, "name": f"daalu {slug}"}


def _created_permission(name: str, pid: str = "perm-uuid-1") -> dict:
    return {"id": pid, "name": name}


def _created_token(key: str = "shiny-new-token") -> dict:
    return {"id": "token-uuid-1", "key": key}


def _install_admin_mock(
    monkeypatch,
    routes: list[tuple[str, str, dict | None, int]] | None = None,
    *,
    free_form: dict[tuple[str, str], dict] | None = None,
) -> list[httpx.Request]:
    """Mount an httpx.MockTransport into the provisioning client factory.

    Two ways to drive it:
      - free_form: dict of (method, path) → response body. Each call
        returns the matching body (200 OK). Path matched exactly.
      - routes: ordered list of (method, path-substr, body, status).
        Each call pops the next matching entry — useful when the same
        path returns different bodies on first vs second call (e.g.,
        find-or-create's GET-then-POST pair).

    Returns the captured request list so tests can assert on bodies.
    """
    seen: list[httpx.Request] = []
    free = dict(free_form or {})
    ordered = list(routes or [])

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        # First try free-form
        body = free.get((req.method, req.url.path))
        if body is not None:
            return httpx.Response(200, json=body)
        # Then try ordered
        for i, (m, path_substr, b, status) in enumerate(ordered):
            if m == req.method and path_substr in req.url.path:
                ordered.pop(i)
                if b is None:
                    return httpx.Response(status, text="error")
                return httpx.Response(status, json=b)
        return httpx.Response(
            404, json={"error": f"unrouted {req.method} {req.url.path}"}
        )

    transport = httpx.MockTransport(handler)

    def _build(url: str, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=url.rstrip("/"),
            transport=transport,
            headers={"Authorization": f"Token {token}"},
        )

    monkeypatch.setattr(
        "daalu_automation.core.sot.nautobot_provisioning._build_admin_client",
        _build,
    )
    return seen


# ---------------------------------------------------------------------------
# 1. Happy path — provision creates Tenant + ObjectPermission + APIToken
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_provision_tenant_happy_path(monkeypatch):
    from daalu_automation.core.sot.nautobot_provisioning import (
        GRANTED_ACTIONS,
        GRANTED_OBJECT_TYPES,
        provision_tenant,
    )

    seen = _install_admin_mock(
        monkeypatch,
        routes=[
            ("GET", "/api/users/users/", _user_lookup(), 200),
            # find-or-create tenant: first GET returns empty, then POST creates
            ("GET", "/api/tenancy/tenants/", _empty_results(), 200),
            ("POST", "/api/tenancy/tenants/", _created_tenant("acme"), 201),
            # find-or-create permission: GET empty, POST creates
            ("GET", "/api/users/permissions/", _empty_results(), 200),
            (
                "POST",
                "/api/users/permissions/",
                _created_permission("daalu-tenant-acme"),
                201,
            ),
            ("POST", "/api/users/tokens/", _created_token("shiny-new"), 201),
        ],
    )

    out = await provision_tenant(
        tenant_slug="acme",
        tenant_name="Acme Corp",
        managed_url="https://managed-nautobot.test",
        admin_token="admin-token",
        service_user="daalu-platform",
    )
    assert out.url == "https://managed-nautobot.test"
    assert out.token == "shiny-new"
    assert out.tenant_slug == "acme"
    assert out.tenant_id == "tenant-uuid-1"
    assert out.permission_id == "perm-uuid-1"

    # Pin the permission body — this is the security boundary.
    perm_post = next(
        r for r in seen if r.method == "POST" and "/api/users/permissions/" in r.url.path
    )
    import json as _json
    body = _json.loads(perm_post.content)
    assert body["constraints"] == {"tenant": "tenant-uuid-1"}, (
        "ObjectPermission constraint is the *only* gate keeping tenant A "
        "out of tenant B's data — it must filter by Nautobot Tenant UUID "
        "(Nautobot 2.x removed the writable `slug` field, and the UUID "
        "is stable across UI renames)"
    )
    assert body["users"] == ["user-uuid-1"]
    assert set(body["actions"]) == set(GRANTED_ACTIONS)
    assert set(body["object_types"]) == set(GRANTED_OBJECT_TYPES)
    assert body["enabled"] is True

    # Pin the tenant lookup shape — Nautobot 2.x rejects `?slug=` with
    # 400 "Unknown filter field", so the find-or-create must filter by
    # name. If this regresses, every provision call 500s.
    tenant_get = next(
        r for r in seen if r.method == "GET" and "/api/tenancy/tenants/" in r.url.path
    )
    assert "name=" in str(tenant_get.url), (
        "tenant lookup must use ?name= filter — Nautobot 2.x dropped "
        "the slug filter on the Tenant API"
    )
    assert "slug=" not in str(tenant_get.url)

    # Pin the tenant create body — must not include `slug` (Nautobot
    # 2.x has no writable slug on Tenant).
    tenant_post = next(
        r for r in seen if r.method == "POST" and "/api/tenancy/tenants/" in r.url.path
    )
    tenant_body = _json.loads(tenant_post.content)
    assert "slug" not in tenant_body, (
        "Nautobot 2.x Tenant has no writable `slug` field; posting "
        "one would either be ignored silently or 400 depending on the "
        "API's strict-mode setting"
    )
    assert tenant_body["name"] == "Acme Corp"


# ---------------------------------------------------------------------------
# 2. Idempotency — re-provisioning reuses existing Tenant + Permission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_provision_is_idempotent_on_tenant_and_permission(monkeypatch):
    from daalu_automation.core.sot.nautobot_provisioning import provision_tenant

    seen = _install_admin_mock(
        monkeypatch,
        routes=[
            ("GET", "/api/users/users/", _user_lookup(), 200),
            # Tenant already exists
            (
                "GET",
                "/api/tenancy/tenants/",
                {"results": [_created_tenant("acme", "t1")]},
                200,
            ),
            # Permission already exists
            (
                "GET",
                "/api/users/permissions/",
                {"results": [_created_permission("daalu-tenant-acme", "p1")]},
                200,
            ),
            ("POST", "/api/users/tokens/", _created_token("rotated"), 201),
        ],
    )

    out = await provision_tenant(
        tenant_slug="acme",
        tenant_name="Acme Corp",
        managed_url="https://managed-nautobot.test",
        admin_token="admin-token",
    )
    assert out.tenant_id == "t1"
    assert out.permission_id == "p1"
    assert out.token == "rotated"
    # No POST to /tenants/ or /permissions/ — we reused both.
    methods_paths = [(r.method, r.url.path) for r in seen]
    assert ("POST", "/api/tenancy/tenants/") not in methods_paths
    assert ("POST", "/api/users/permissions/") not in methods_paths


# ---------------------------------------------------------------------------
# 3. Missing service user → loud failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_provision_fails_when_service_user_missing(monkeypatch):
    from daalu_automation.core.sot.nautobot_provisioning import (
        ProvisioningError,
        provision_tenant,
    )

    _install_admin_mock(
        monkeypatch,
        free_form={
            ("GET", "/api/users/users/"): _empty_results(),
        },
    )

    with pytest.raises(ProvisioningError, match="daalu-platform"):
        await provision_tenant(
            tenant_slug="acme",
            tenant_name="Acme",
            managed_url="https://managed-nautobot.test",
            admin_token="admin-token",
        )


# ---------------------------------------------------------------------------
# 4. Token mint returns no key → loud failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_provision_fails_when_token_returns_no_key(monkeypatch):
    from daalu_automation.core.sot.nautobot_provisioning import (
        ProvisioningError,
        provision_tenant,
    )

    _install_admin_mock(
        monkeypatch,
        routes=[
            ("GET", "/api/users/users/", _user_lookup(), 200),
            ("GET", "/api/tenancy/tenants/", _empty_results(), 200),
            ("POST", "/api/tenancy/tenants/", _created_tenant("acme"), 201),
            ("GET", "/api/users/permissions/", _empty_results(), 200),
            (
                "POST",
                "/api/users/permissions/",
                _created_permission("daalu-tenant-acme"),
                201,
            ),
            # Token response with no "key" — should fail.
            ("POST", "/api/users/tokens/", {"id": "x"}, 201),
        ],
    )

    with pytest.raises(ProvisioningError, match="no token key"):
        await provision_tenant(
            tenant_slug="acme",
            tenant_name="Acme",
            managed_url="https://managed-nautobot.test",
            admin_token="admin-token",
        )


# ---------------------------------------------------------------------------
# 5. Hosted-status endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_hosted_status_off_when_unconfigured(client, seeded):
    from daalu_automation.config import get_settings

    # Default test config has empty managed_nautobot_url
    settings = get_settings()
    assert not settings.managed_nautobot_url

    resp = await client.get(
        "/api/v1/onboarding/nautobot/hosted-status",
        headers=auth_headers(seeded.token_a),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["hosted_available"] is False
    assert "BYO" in body["detail"] or "not configured" in body["detail"]


@pytest.mark.asyncio(loop_scope="session")
async def test_hosted_status_on_when_configured(client, seeded, monkeypatch):
    from daalu_automation.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "managed_nautobot_url", "https://m.test")
    monkeypatch.setattr(settings, "managed_nautobot_admin_token", "tok")

    resp = await client.get(
        "/api/v1/onboarding/nautobot/hosted-status",
        headers=auth_headers(seeded.token_a),
    )
    assert resp.status_code == 200
    assert resp.json()["hosted_available"] is True


# ---------------------------------------------------------------------------
# 6. Provision route — full HTTP round-trip writes encrypted integration row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_provision_route_writes_encrypted_integration_row(
    db, client, seeded, monkeypatch,
):
    from daalu_automation.config import get_settings
    from daalu_automation.core.crypto import decrypt_secret
    from daalu_automation.models import Integration

    settings = get_settings()
    monkeypatch.setattr(settings, "managed_nautobot_url", "https://m.test")
    monkeypatch.setattr(settings, "managed_nautobot_admin_token", "tok")

    _install_admin_mock(
        monkeypatch,
        routes=[
            ("GET", "/api/users/users/", _user_lookup(), 200),
            ("GET", "/api/tenancy/tenants/", _empty_results(), 200),
            ("POST", "/api/tenancy/tenants/", _created_tenant("tenant-a"), 201),
            ("GET", "/api/users/permissions/", _empty_results(), 200),
            (
                "POST",
                "/api/users/permissions/",
                _created_permission("daalu-tenant-tenant-a"),
                201,
            ),
            ("POST", "/api/users/tokens/", _created_token("provisioned-tok"), 201),
        ],
    )

    resp = await client.post(
        "/api/v1/onboarding/nautobot/provision",
        headers=auth_headers(seeded.token_a),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["nautobot_tenant_slug"] == "tenant-a"
    assert body["url"] == "https://m.test"

    # Integration row landed; token is encrypted.
    row = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == seeded.tenant_a_id,
                Integration.provider == "nautobot",
            )
        )
    ).scalar_one()
    assert row.config["url"] == "https://m.test"
    assert row.config["hosted"] is True
    assert row.config["nautobot_tenant_slug"] == "tenant-a"
    assert row.config["nautobot_tenant_id"] == "tenant-uuid-1"
    assert row.config["permission_id"] == "perm-uuid-1"
    # Token stored encrypted, never plaintext.
    assert "token" not in row.config or row.config.get("token") != "provisioned-tok"
    assert decrypt_secret(row.config["token_ciphertext"]) == "provisioned-tok"


# ---------------------------------------------------------------------------
# 7. Provision route — 503 when hosted mode is disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_provision_route_503_when_disabled(client, seeded):
    resp = await client.post(
        "/api/v1/onboarding/nautobot/provision",
        headers=auth_headers(seeded.token_a),
    )
    assert resp.status_code == 503
    assert "not configured" in resp.text


# ---------------------------------------------------------------------------
# 8. Provision route — idempotent (second call upserts, doesn't duplicate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_provision_route_is_idempotent(
    db, client, seeded, monkeypatch,
):
    from daalu_automation.config import get_settings
    from daalu_automation.models import Integration

    settings = get_settings()
    monkeypatch.setattr(settings, "managed_nautobot_url", "https://m.test")
    monkeypatch.setattr(settings, "managed_nautobot_admin_token", "tok")

    def routes_for_call() -> list[tuple[str, str, dict | None, int]]:
        return [
            ("GET", "/api/users/users/", _user_lookup(), 200),
            (
                "GET",
                "/api/tenancy/tenants/",
                {"results": [_created_tenant("tenant-a", "t-existing")]},
                200,
            ),
            (
                "GET",
                "/api/users/permissions/",
                {
                    "results": [
                        _created_permission("daalu-tenant-tenant-a", "p-existing")
                    ]
                },
                200,
            ),
            ("POST", "/api/users/tokens/", _created_token("call-1"), 201),
        ]

    _install_admin_mock(monkeypatch, routes=routes_for_call())
    resp = await client.post(
        "/api/v1/onboarding/nautobot/provision",
        headers=auth_headers(seeded.token_a),
    )
    assert resp.status_code == 200

    # Second call — re-install transport with the same canned responses.
    _install_admin_mock(monkeypatch, routes=routes_for_call())
    resp = await client.post(
        "/api/v1/onboarding/nautobot/provision",
        headers=auth_headers(seeded.token_a),
    )
    assert resp.status_code == 200

    rows = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == seeded.tenant_a_id,
                Integration.provider == "nautobot",
            )
        )
    ).scalars().all()
    # Exactly one row, not two. Upsert preserved.
    assert len(rows) == 1
    assert rows[0].config["nautobot_tenant_id"] == "t-existing"
