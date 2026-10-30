"""Keycloak hands back tokens that are already dead — the hub must notice.

Once a user's SSO session is older than the realm's ``ssoSessionMaxLifespan``,
Keycloak clamps a token-exchanged access token's ``exp`` to the *end of that
session* and returns it with **HTTP 200** and a negative ``expires_in``. Nothing
about the call looks like a failure, so without an explicit check the hub caches
the corpse and injects it — and every NV-CM tool answers ``401 Jwt is expired``
until the user happens to log in again.

Two guards are pinned here: the mint-time check (which routes the caller into
its existing service-token fallback), and the realm ceiling that stops Keycloak
producing such a token in the first place.
"""

from __future__ import annotations

import types

import httpx
import pytest

from daalu_automation.core import keycloak as kc
from daalu_automation.core.keycloak_admin import KeycloakAdmin

_TOKEN_URL = "https://auth.example.test/realms/daalu/protocol/openid-connect/token"


def _install(monkeypatch, module, handler) -> list[httpx.Request]:
    """Route ``module``'s httpx calls into ``handler``; return captured requests."""
    seen: list[httpx.Request] = []

    def _capture(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    transport = httpx.MockTransport(_capture)

    def _client(*a, **kw):
        kw.pop("proxy", None)
        return httpx.AsyncClient(*a, transport=transport, **kw)

    shim = types.SimpleNamespace(AsyncClient=_client, HTTPError=httpx.HTTPError)
    monkeypatch.setattr(module, "httpx", shim)
    return seen


@pytest.fixture(autouse=True)
def _clear_cache():
    kc.clear_token_cache()
    yield
    kc.clear_token_cache()


def _exchange_handler(expires_in: float):
    """Keycloak: refresh succeeds, exchange returns a token with this lifetime."""

    def handler(req: httpx.Request) -> httpx.Response:
        grant = httpx.QueryParams(req.content.decode()).get("grant_type")
        if grant == "refresh_token":
            return httpx.Response(200, json={"access_token": "subject-token"})
        return httpx.Response(
            200, json={"access_token": "exchanged-token", "expires_in": expires_in}
        )

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_in", [-16, 0, 5])
async def test_already_expired_exchange_raises_and_is_not_cached(
    monkeypatch, expires_in
):
    """A non-positive (or within-skew) lifetime is a failure, not a token."""
    seen = _install(monkeypatch, kc, _exchange_handler(expires_in))

    for _ in range(2):
        with pytest.raises(kc.KeycloakAuthError, match="already-expired"):
            await kc.get_user_nvcm_token(
                refresh_token="offline-rt",
                nvcm_client_id="daalu-hub-nvcm",
                nvcm_client_secret="s3cret",
                token_url=_TOKEN_URL,
            )
    # Second call re-hit Keycloak: nothing was cached (2 calls × refresh+exchange).
    assert len(seen) == 4


@pytest.mark.asyncio
async def test_live_exchange_is_returned_and_cached(monkeypatch):
    """The healthy path is untouched — mint once, serve from cache after."""
    seen = _install(monkeypatch, kc, _exchange_handler(300))

    for _ in range(2):
        token = await kc.get_user_nvcm_token(
            refresh_token="offline-rt",
            nvcm_client_id="daalu-hub-nvcm",
            nvcm_client_secret="s3cret",
            token_url=_TOKEN_URL,
        )
        assert token == "exchanged-token"
    assert len(seen) == 2  # one refresh + one exchange, then cached


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "wanted", "expect_put"),
    [(36000, 90000, True), (90000, 90000, False), (2592000, 90000, False)],
)
async def test_sso_max_lifespan_raised_but_never_lowered(
    monkeypatch, current, wanted, expect_put
):
    """Raise the realm ceiling when it's too low; leave a higher one alone."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://auth.example.test/realms/daalu")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "admin-cli")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "admin-secret")
    from daalu_automation.config import get_settings

    get_settings.cache_clear()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "admin-token"})
        if req.method == "GET":
            return httpx.Response(
                200, json={"realm": "daalu", "ssoSessionMaxLifespan": current}
            )
        return httpx.Response(204)

    import daalu_automation.core.keycloak_admin as kca

    seen = _install(monkeypatch, kca, handler)
    changed = await KeycloakAdmin().ensure_sso_session_max_lifespan(seconds=wanted)

    assert changed is expect_put
    puts = [r for r in seen if r.method == "PUT"]
    assert bool(puts) is expect_put
    if expect_put:
        # A full representation goes back, not a one-key patch that would drop
        # every other realm setting.
        import json

        body = json.loads(puts[0].content)
        assert body["realm"] == "daalu"
        assert body["ssoSessionMaxLifespan"] == wanted

    get_settings.cache_clear()
