"""Device-flow auth + public coding gateway.

Exercises the two `daalu login` flows end-to-end through the real ASGI app:
the browser device grant (code → approve → poll → PAT) and the express
paste-command lane. Then verifies the minted PAT actually authenticates the
public coding endpoint, and that the coding proxy enforces eligibility +
upstream config.
"""

from __future__ import annotations

import httpx
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import get_settings
from daalu_automation.core import cli_auth
from daalu_automation.models.tenant import Tenant
from tests.conftest import auth_headers


async def _make_eligible(db: AsyncSession, tenant_id) -> None:
    """Flip the operator override so the tenant can serve coding prompts."""
    tenant = await db.get(Tenant, tenant_id)
    flags = dict(tenant.feature_flags or {})
    flags["workspaces_enabled"] = True
    tenant.feature_flags = flags
    await db.commit()


# ── pure helpers ────────────────────────────────────────────────────────────


def test_user_code_normalisation_round_trips():
    code = cli_auth.generate_user_code()
    assert code[4] == "-" and len(code) == 9
    # All the shapes a human might type collapse to the canonical form.
    assert cli_auth.normalize_user_code(code.lower()) == code
    assert cli_auth.normalize_user_code(code.replace("-", " ")) == code
    assert cli_auth.normalize_user_code(code.replace("-", "")) == code


def test_device_code_is_hashed_not_stored_plain():
    code = cli_auth.generate_device_code()
    assert code.startswith("dvc_live_")
    digest = cli_auth.hash_device_code(code)
    assert digest != code and len(digest) == 64


# ── device flow (browser grant) ─────────────────────────────────────────────


async def test_device_flow_end_to_end(client, seeded, db):
    # 1) CLI starts a flow — no auth required.
    start = await client.post(
        "/api/v1/cli/device/code", json={"client_name": "daalu-cli @ test-laptop"}
    )
    assert start.status_code == 200, start.text
    body = start.json()
    device_code = body["device_code"]
    user_code = body["user_code"]
    assert body["verification_uri_complete"].endswith(f"?code={user_code}")

    # 2) Polling before approval → pending.
    poll = await client.post(
        "/api/v1/cli/device/token", json={"device_code": device_code}
    )
    assert poll.status_code == 200
    assert poll.json()["status"] == "authorization_pending"

    # 3) The browser (logged-in user) sees the request, then approves it.
    pending = await client.get(
        "/api/v1/cli/device/pending",
        params={"user_code": user_code.lower()},  # case-insensitive
        headers=auth_headers(seeded.token_a),
    )
    assert pending.status_code == 200
    assert pending.json()["client_name"] == "daalu-cli @ test-laptop"

    approve = await client.post(
        "/api/v1/cli/device/approve",
        json={"user_code": user_code},
        headers=auth_headers(seeded.token_a),
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"

    # 4) CLI polls again → gets a PAT.
    redeem = await client.post(
        "/api/v1/cli/device/token", json={"device_code": device_code}
    )
    assert redeem.status_code == 200
    rb = redeem.json()
    assert rb["status"] == "approved"
    token = rb["access_token"]
    assert token.startswith("dpat_")
    assert rb["token_type"] == "pat"

    # 5) The minted PAT authenticates the API as user A.
    await _make_eligible(db, seeded.tenant_a_id)
    me = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert me.status_code == 200
    assert me.json()["id"] == str(seeded.user_a_id)

    # 6) A second redeem of the same code is refused (single-use).
    again = await client.post(
        "/api/v1/cli/device/token", json={"device_code": device_code}
    )
    assert again.json()["status"] == "denied"


async def test_unknown_device_code_is_denied_not_500(client):
    r = await client.post(
        "/api/v1/cli/device/token", json={"device_code": "dvc_live_nope"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "denied"


async def test_login_command_express_flow(client, seeded):
    # The workspace page mints a pre-approved command for the logged-in user.
    mint = await client.post(
        "/api/v1/cli/login-command", headers=auth_headers(seeded.token_a)
    )
    assert mint.status_code == 200
    cmd = mint.json()
    assert cmd["command"].startswith("daalu login --code dvc_live_")
    device_code = cmd["device_code"]

    # The CLI redeems it directly — no browser approval step.
    redeem = await client.post(
        "/api/v1/cli/device/token", json={"device_code": device_code}
    )
    assert redeem.json()["status"] == "approved"
    assert redeem.json()["access_token"].startswith("dpat_")


async def test_login_command_requires_auth(client):
    r = await client.post("/api/v1/cli/login-command")
    assert r.status_code == 401


# ── public coding gateway ───────────────────────────────────────────────────


async def test_coding_config_lists_models_when_eligible(client, seeded, db):
    await _make_eligible(db, seeded.tenant_a_id)
    r = await client.get(
        "/api/v1/coding/config", headers=auth_headers(seeded.token_a)
    )
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["chat_completions_path"] == "/api/v1/coding/v1/chat/completions"
    assert cfg["default_model"].startswith("qwen/")
    assert any(m["available"] for m in cfg["models"])


async def test_coding_config_403_when_not_eligible(client, seeded, db):
    # Make sure tenant B has no eligibility path.
    tenant = await db.get(Tenant, seeded.tenant_b_id)
    tenant.feature_flags = {}
    tenant.sovereign_inference_url = None
    await db.commit()
    r = await client.get(
        "/api/v1/coding/config", headers=auth_headers(seeded.token_b)
    )
    assert r.status_code == 403


async def test_coding_completion_requires_messages(client, seeded, db):
    await _make_eligible(db, seeded.tenant_a_id)
    r = await client.post(
        "/api/v1/coding/v1/chat/completions",
        headers=auth_headers(seeded.token_a),
        json={"model": "x"},  # no messages
    )
    assert r.status_code == 422


async def test_coding_completion_503_without_upstream(client, seeded, db, monkeypatch):
    await _make_eligible(db, seeded.tenant_a_id)
    s = get_settings()
    monkeypatch.setattr(s, "daalu_hosted_gateway_url", "", raising=False)
    monkeypatch.setattr(s, "llm_local_base_url", "", raising=False)
    r = await client.post(
        "/api/v1/coding/v1/chat/completions",
        headers=auth_headers(seeded.token_a),
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 503


@respx.mock
async def test_coding_completion_proxies_to_local_vllm(client, seeded, db, monkeypatch):
    await _make_eligible(db, seeded.tenant_a_id)
    s = get_settings()
    monkeypatch.setattr(s, "daalu_hosted_gateway_url", "", raising=False)
    monkeypatch.setattr(
        s, "llm_local_base_url", "http://vllm.local/v1", raising=False
    )
    route = respx.post("http://vllm.local/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                "model": "qwen/qwen3-coder-30b-a3b-instruct",
            },
        )
    )
    r = await client.post(
        "/api/v1/coding/v1/chat/completions",
        headers=auth_headers(seeded.token_a),
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "hello"
    assert route.called
    # The CLI sent no model → server defaulted to the configured served name.
    sent = route.calls.last.request
    assert b"qwen/" in sent.content
