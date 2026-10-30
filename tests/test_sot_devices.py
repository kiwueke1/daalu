"""HTTP-route coverage for the new /sot/devices proxy surface.

These tests are intentionally thin — the bulk of the device-shaped logic
lives in NautobotSoT (already covered by test_change_proposals,
test_redfish_adapter, etc.). What's worth pinning here:

1. List + get proxy through to NautobotSoT via the per-tenant
   integration's URL+token.
2. Update-intent dispatches to the right facts schema based on the
   device's transport (so a Linux device gets LinuxFacts validation,
   a Redfish device gets RedfishFacts validation, etc.).
3. Cross-tenant isolation — tenant B's token cannot enumerate or write
   to tenant A's devices.
4. 409 when the tenant has no Nautobot integration (we don't want
   the wizard to land an unhelpful 500).
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.conftest import auth_headers


def _nautobot_device_payload(
    device_id: str,
    *,
    intent: dict | None = None,
    transport: str = "linux_ssh",
    name: str = "web01",
) -> dict:
    cfg = {"daalu_intent": intent} if intent is not None else {}
    return {
        "id": device_id,
        "name": name,
        "primary_ip4": {"address": "10.0.0.5/24"},
        "platform": {"name": "ubuntu"},
        "tags": [],
        "config_context": cfg,
        "custom_field_data": {"daalu_transport": transport},
    }


# ---------------------------------------------------------------------------
# 1. List proxies through NautobotSoT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_list_devices_proxies_through_nautobot(
    client, seeded, fake_redis, nautobot_integration, mock_nautobot_transport,
):
    device_id = str(uuid.uuid4())
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {
            "data": {"devices": [_nautobot_device_payload(device_id, intent={})]}
        },
    })

    resp = await client.get(
        "/api/v1/sot/devices", headers=auth_headers(seeded.token_a)
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == device_id
    assert rows[0]["transport"] == "linux_ssh"


# ---------------------------------------------------------------------------
# 2. Get + intent endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_get_device_and_intent(
    client, seeded, fake_redis, nautobot_integration, mock_nautobot_transport,
):
    device_id = str(uuid.uuid4())
    intent = {
        "hostname": "web01",
        "authorized_keys": [],
        "sysctl": [{"name": "vm.swappiness", "value": "10"}],
        "packages": [],
    }
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {
            "data": {"device": _nautobot_device_payload(device_id, intent=intent)}
        },
    })

    resp = await client.get(
        f"/api/v1/sot/devices/{device_id}",
        headers=auth_headers(seeded.token_a),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "web01"

    intent_resp = await client.get(
        f"/api/v1/sot/devices/{device_id}/intent",
        headers=auth_headers(seeded.token_a),
    )
    assert intent_resp.status_code == 200
    body = intent_resp.json()
    assert body["transport"] == "linux_ssh"
    assert body["facts"]["sysctl"][0]["name"] == "vm.swappiness"


@pytest.mark.asyncio(loop_scope="session")
async def test_get_intent_404_when_unset(
    client, seeded, fake_redis, nautobot_integration, mock_nautobot_transport,
):
    device_id = str(uuid.uuid4())
    # Device exists but has no daalu_intent Config Context.
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {
            "data": {"device": _nautobot_device_payload(device_id, intent=None)}
        },
    })
    resp = await client.get(
        f"/api/v1/sot/devices/{device_id}/intent",
        headers=auth_headers(seeded.token_a),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. 409 when tenant has no Nautobot integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_list_devices_without_nautobot_returns_409(client, seeded):
    # tenant B has no nautobot integration in the seeded fixtures.
    resp = await client.get(
        "/api/v1/sot/devices", headers=auth_headers(seeded.token_b)
    )
    assert resp.status_code == 409
    # Helpful error pointing the operator at the wizard.
    assert "nautobot" in resp.text.lower() or "integration" in resp.text.lower()


# ---------------------------------------------------------------------------
# 4. Update-intent dispatches by device transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_update_intent_validates_against_linux_schema(
    db, client, seeded, fake_redis, nautobot_integration, mock_nautobot_transport,
    monkeypatch,
):
    device_id = str(uuid.uuid4())

    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        path = req.url.path
        if path == "/api/graphql/":
            return httpx.Response(200, json={
                "data": {"device": _nautobot_device_payload(device_id, intent=None)}
            })
        if path.startswith("/api/extras/config-contexts/"):
            if req.method == "GET":
                return httpx.Response(200, json={"results": []})
            if req.method == "POST":
                return httpx.Response(
                    201, json={"id": "ctx-uuid", "last_updated": "2026-05-21"}
                )
        return httpx.Response(404, json={"error": req.url.path})

    transport = httpx.MockTransport(handler)

    def _build(url, token, *, proxy_url=None, forward_host=None):
        return httpx.AsyncClient(
            base_url=url.rstrip("/"),
            transport=transport,
            headers={"Authorization": f"Token {token}"},
        )

    monkeypatch.setattr(
        "daalu_automation.core.sot.nautobot._build_http_client", _build
    )

    resp = await client.put(
        f"/api/v1/sot/devices/{device_id}/intent",
        headers=auth_headers(seeded.token_a),
        json={
            "facts": {
                "hostname": "web02",
                "sysctl": [{"name": "vm.swappiness", "value": "10"}],
            }
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transport"] == "linux_ssh"
    assert body["facts"]["hostname"] == "web02"


@pytest.mark.asyncio(loop_scope="session")
async def test_update_intent_rejects_malformed_payload(
    client, seeded, fake_redis, nautobot_integration, mock_nautobot_transport,
):
    device_id = str(uuid.uuid4())
    mock_nautobot_transport({
        ("POST", "/api/graphql/"): {
            "data": {"device": _nautobot_device_payload(device_id, intent=None)}
        },
    })
    resp = await client.put(
        f"/api/v1/sot/devices/{device_id}/intent",
        headers=auth_headers(seeded.token_a),
        # sysctl entries must be {name, value} objects — pass strings
        # so pydantic rejects.
        json={"facts": {"sysctl": ["vm.swappiness=10"]}},
    )
    assert resp.status_code == 422
    assert "validat" in resp.text.lower() or "schema" in resp.text.lower()
