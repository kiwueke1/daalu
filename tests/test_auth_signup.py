"""Self-service local signup + email verification.

Covers the ``/auth/signup`` → email → ``/auth/verify-email`` flow added so
visitors can create a minimal-access local account alongside Google SSO.
Key invariants: signup is gated by ``self_signup_enabled``; a new account is
inactive, non-admin, and lands in its OWN isolated tenant; the account can't
log in until the emailed token is redeemed; signup never leaks whether an
email is already registered.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select

from daalu_automation.config import DEFAULT_TENANT_ID, get_settings
from daalu_automation.models import EmailVerificationToken, Tenant, User

SIGNUP = "/api/v1/auth/signup"
VERIFY = "/api/v1/auth/verify-email"
RESEND = "/api/v1/auth/verify-email/resend"


@pytest.fixture
def signup_enabled(monkeypatch):
    """Flip on self-service signup for the duration of a test."""
    settings = get_settings()
    monkeypatch.setattr(settings, "self_signup_enabled", True)
    return settings


@pytest.fixture
def captured_links(monkeypatch):
    """Intercept the verification email and capture its link per recipient.

    Returns a dict ``{email: verify_url}`` the test can read the token out of,
    and avoids any real SMTP/DB email path.
    """
    links: dict[str, str] = {}

    async def fake_send(*, to_email, verify_url, **_kw):
        links[to_email] = verify_url
        return True

    monkeypatch.setattr(
        "daalu_automation.core.signup.send_verification_email", fake_send
    )
    return links


def _token_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["token"][0]


@pytest.mark.asyncio(loop_scope="session")
async def test_signup_disabled_returns_404(client):
    """With the flag off (default), signup is not reachable."""
    r = await client.post(SIGNUP, json={"email": "x@example.com", "password": "supersecret1"})
    assert r.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_signup_creates_inactive_isolated_user(
    signup_enabled, captured_links, client, db
):
    r = await client.post(
        SIGNUP,
        json={
            "email": "New.User@Example.com",
            "password": "supersecret1",
            "full_name": "New User",
            "organization": "Acme",
        },
    )
    assert r.status_code == 202
    assert r.json() == {"status": "verification_sent"}

    user = (
        await db.execute(select(User).where(User.email == "new.user@example.com"))
    ).scalar_one()
    assert user.is_active is False
    assert user.is_admin is False
    assert user.is_superuser is False
    assert user.email_verified_at is None
    assert user.hashed_password  # password was set
    # Isolated in its own brand-new tenant — never the default/bootstrap one.
    assert user.tenant_id != DEFAULT_TENANT_ID
    tenant = await db.get(Tenant, user.tenant_id)
    assert tenant is not None and tenant.name == "Acme"

    # A verification token row exists, and the email was "sent".
    n = (
        await db.execute(
            select(func.count()).select_from(EmailVerificationToken).where(
                EmailVerificationToken.user_id == user.id
            )
        )
    ).scalar_one()
    assert n == 1
    assert "new.user@example.com" in captured_links


@pytest.mark.asyncio(loop_scope="session")
async def test_login_blocked_until_verified(signup_enabled, captured_links, client):
    await client.post(
        SIGNUP, json={"email": "pending@example.com", "password": "supersecret1"}
    )
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "pending@example.com", "password": "supersecret1"},
    )
    # Account exists but is inactive → 403, not a 200 session.
    assert r.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_verify_activates_and_logs_in(
    signup_enabled, captured_links, client, db
):
    await client.post(
        SIGNUP, json={"email": "verify@example.com", "password": "supersecret1"}
    )
    token = _token_from(captured_links["verify@example.com"])

    r = await client.post(VERIFY, json={"token": token})
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == "verify@example.com"
    assert body["user"]["is_admin"] is False
    # Session cookie was set, so the browser is logged in.
    assert get_settings().auth_cookie_name in r.cookies

    user = (
        await db.execute(select(User).where(User.email == "verify@example.com"))
    ).scalar_one()
    assert user.is_active is True
    assert user.email_verified_at is not None

    # Single-use: a replay of the same token now fails.
    again = await client.post(VERIFY, json={"token": token})
    assert again.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_verified_user_sees_only_own_empty_tenant(
    signup_enabled, captured_links, client
):
    await client.post(
        SIGNUP, json={"email": "iso@example.com", "password": "supersecret1"}
    )
    token = _token_from(captured_links["iso@example.com"])
    login = await client.post(VERIFY, json={"token": token})
    bearer = {"Authorization": f"Bearer {login.json()['token']}"}

    me = await client.get("/api/v1/auth/me", headers=bearer)
    assert me.status_code == 200
    assert me.json()["tenant_id"] != str(DEFAULT_TENANT_ID)

    # Tenant-scoped read returns this tenant's (empty) data, not anyone else's.
    events = await client.get("/api/v1/events", headers=bearer)
    assert events.status_code == 200
    assert events.json() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_duplicate_email_is_neutral(signup_enabled, captured_links, client, db):
    body = {"email": "dup@example.com", "password": "supersecret1"}
    r1 = await client.post(SIGNUP, json=body)
    r2 = await client.post(SIGNUP, json=body)
    assert r1.status_code == 202
    assert r2.status_code == 202  # same neutral response, no enumeration

    users = (
        await db.execute(
            select(func.count()).select_from(User).where(User.email == "dup@example.com")
        )
    ).scalar_one()
    assert users == 1  # second signup created nothing


@pytest.mark.asyncio(loop_scope="session")
async def test_resend_issues_fresh_token(signup_enabled, captured_links, client, db):
    await client.post(
        SIGNUP, json={"email": "resend@example.com", "password": "supersecret1"}
    )
    r = await client.post(RESEND, json={"email": "resend@example.com"})
    assert r.status_code == 202

    user = (
        await db.execute(select(User).where(User.email == "resend@example.com"))
    ).scalar_one()
    n = (
        await db.execute(
            select(func.count()).select_from(EmailVerificationToken).where(
                EmailVerificationToken.user_id == user.id
            )
        )
    ).scalar_one()
    assert n == 2  # original + resent


@pytest.mark.asyncio(loop_scope="session")
async def test_config_advertises_signup_when_enabled(signup_enabled, client):
    cfg = (await client.get("/api/v1/auth/config")).json()
    assert cfg["self_signup_enabled"] is True
    assert cfg["password_login_enabled"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_config_default_no_signup(client):
    cfg = (await client.get("/api/v1/auth/config")).json()
    assert cfg["self_signup_enabled"] is False


@pytest.fixture
def native_login_enabled(monkeypatch):
    """oidc mode + native password login on, but public signup OFF —
    the production posture for ops.daalu.io (operator/invited accounts can
    log in with a password alongside Google SSO; the public cannot register).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(settings, "native_password_login_enabled", True)
    monkeypatch.setattr(settings, "self_signup_enabled", False)
    return settings


@pytest.mark.asyncio(loop_scope="session")
async def test_native_login_decoupled_from_public_signup(native_login_enabled, client):
    """``native_password_login_enabled`` shows the password form and permits
    /login WITHOUT opening public /signup."""
    cfg = (await client.get("/api/v1/auth/config")).json()
    assert cfg["mode"] == "oidc"
    assert cfg["password_login_enabled"] is True  # login form renders
    assert cfg["self_signup_enabled"] is False  # but no "create account" link

    # Public self-service signup stays closed.
    r = await client.post(SIGNUP, json={"email": "x@example.com", "password": "supersecret1"})
    assert r.status_code == 404

    # Login is NOT blanket-disabled: the gate passes and we fall through to
    # credential checking → 401 for an unknown user (not 403 "login disabled").
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "whatever12"},
    )
    assert r.status_code == 401
