"""Credential resolution for the tenant's Nautobot (SoT + bulk import).

Pins the NV-CM-bundled-Nautobot transport contract from
docs/design/nv-config-manager-integration.md §17:

1. A ``config_manager`` integration carrying a token is preferred over the
   standalone BYO row, and is dialed **via the environment's gateway
   Service address** with the flat ``svc-nautobot.<base>`` host carried as
   a routing header — the flat host is neither publicly resolvable nor in
   the edge proxy's allowlist, so dialing it directly can never work.
2. Without a stamped token the loader falls back to the BYO row unchanged.
3. ``_build_http_client`` sends the routing host as ``X-Daalu-Forward-Host``
   through the edge proxy (which strips ``Host`` as hop-by-hop) and as a
   plain ``Host`` override on a direct dial.
4. Naming an environment resolves *that* environment's bundled Nautobot off
   its own row, and fails closed rather than answering with the primary's.
"""

from __future__ import annotations

import pytest

from daalu_automation.config_manager_controller import local_admin
from daalu_automation.core.crypto import encrypt_secret
from daalu_automation.core.sot.nautobot import (
    NautobotUnavailable,
    _build_http_client,
    _load_credentials,
)
from daalu_automation.models import (
    ConfigManagerTenant,
    ConfigManagerTenantState,
    Integration,
    IntegrationStatus,
)

_GATEWAY = (
    "http://envoy-cm-default-nv-config-manager-gateway.envoy-gateway-system"
    ".svc.cluster.local"
)
_FLAT_URL = "http://svc-nautobot.production-default.cmtools.test"


async def _add_cm_integration(db, tenant_id, config):
    row = Integration(
        tenant_id=tenant_id,
        provider="config_manager",
        module="infra",
        name="Network management stack",
        status=IntegrationStatus.connected,
        config=config,
    )
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio(loop_scope="session")
async def test_cm_bundled_dials_gateway_with_forward_host(db, seeded):
    await _add_cm_integration(
        db,
        seeded.tenant_a_id,
        {
            "nautobot_url": _FLAT_URL,
            "gateway_internal_url": _GATEWAY,
            "nautobot_token_ciphertext": encrypt_secret("cm-token"),
        },
    )
    url, token, proxy_url, forward_host = await _load_credentials(
        db, seeded.tenant_a_id
    )
    assert url == _GATEWAY
    assert token == "cm-token"
    assert proxy_url is None  # no tunnel bound in this fixture
    assert forward_host == "svc-nautobot.production-default.cmtools.test"


@pytest.mark.asyncio(loop_scope="session")
async def test_cm_without_gateway_falls_back_to_flat_url(db, seeded):
    await _add_cm_integration(
        db,
        seeded.tenant_a_id,
        {
            "nautobot_url": _FLAT_URL,
            "nautobot_token_ciphertext": encrypt_secret("cm-token"),
        },
    )
    url, token, proxy_url, forward_host = await _load_credentials(
        db, seeded.tenant_a_id
    )
    assert url == _FLAT_URL
    assert forward_host is None


@pytest.mark.asyncio(loop_scope="session")
async def test_cm_without_token_falls_back_to_byo(
    db, seeded, nautobot_integration
):
    # A cm row that never got its token stamped must not shadow a working
    # BYO integration.
    await _add_cm_integration(
        db,
        seeded.tenant_a_id,
        {"nautobot_url": _FLAT_URL, "gateway_internal_url": _GATEWAY},
    )
    url, token, proxy_url, forward_host = await _load_credentials(
        db, seeded.tenant_a_id
    )
    assert url == "https://nautobot.test"
    assert token == "dummy-token"
    assert forward_host is None


@pytest.mark.asyncio(loop_scope="session")
async def test_no_integration_raises(db, seeded):
    with pytest.raises(NautobotUnavailable):
        await _load_credentials(db, seeded.tenant_b_id)


# ── Per-environment resolution ────────────────────────────────────────────
#
# The config_manager Integration row only ever carries the PRIMARY
# environment's Nautobot, so a named environment has to come off its own
# config_manager_tenants row or the caller silently gets the wrong fabric.

_LAB_GATEWAY = (
    "http://envoy-cm-default-lab-gateway.envoy-gateway-system.svc.cluster.local"
)
_LAB_FLAT_URL = "http://svc-nautobot.lab-default.cmtools.test"


async def _add_environment(db, tenant_id, *, slug, token=None, **overrides):
    row = ConfigManagerTenant(
        tenant_id=tenant_id,
        name=slug.title(),
        slug=slug,
        is_primary=False,
        state=ConfigManagerTenantState.active,
        namespace=f"cm-default-{slug}",
        base_hostname=f"{slug}-default.cmtools.test",
        urls=overrides.pop("urls", {"nautobot_url": _LAB_FLAT_URL}),
        gateway_internal_url=overrides.pop("gateway_internal_url", _LAB_GATEWAY),
        **overrides,
    )
    if token is not None:
        row.secrets_ciphertext = local_admin.with_api_token(row, token)
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio(loop_scope="session")
async def test_named_environment_uses_its_own_nautobot(db, seeded):
    """The slug wins over the primary's Integration row, token and all."""
    await _add_cm_integration(
        db,
        seeded.tenant_a_id,
        {
            "nautobot_url": _FLAT_URL,
            "gateway_internal_url": _GATEWAY,
            "nautobot_token_ciphertext": encrypt_secret("primary-token"),
        },
    )
    await _add_environment(db, seeded.tenant_a_id, slug="lab", token="lab-token")

    url, token, _proxy, forward_host = await _load_credentials(
        db, seeded.tenant_a_id, "lab"
    )
    assert url == _LAB_GATEWAY
    assert token == "lab-token"
    assert forward_host == "svc-nautobot.lab-default.cmtools.test"

    # …and omitting the slug still resolves the primary, unchanged.
    url, token, _proxy, _fh = await _load_credentials(db, seeded.tenant_a_id)
    assert url == _GATEWAY
    assert token == "primary-token"


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_environment_raises_rather_than_falling_back(db, seeded):
    """Answering with the primary's fabric is the bug, not the fallback."""
    await _add_cm_integration(
        db,
        seeded.tenant_a_id,
        {
            "nautobot_url": _FLAT_URL,
            "gateway_internal_url": _GATEWAY,
            "nautobot_token_ciphertext": encrypt_secret("primary-token"),
        },
    )
    with pytest.raises(NautobotUnavailable, match="no-such-env"):
        await _load_credentials(db, seeded.tenant_a_id, "no-such-env")


@pytest.mark.asyncio(loop_scope="session")
async def test_environment_without_stamped_token_raises(db, seeded):
    """Mid-provision environments report themselves, not the primary."""
    await _add_environment(db, seeded.tenant_a_id, slug="unstamped")
    with pytest.raises(NautobotUnavailable, match="token"):
        await _load_credentials(db, seeded.tenant_a_id, "unstamped")


@pytest.mark.asyncio(loop_scope="session")
async def test_environment_without_gateway_dials_the_flat_url(db, seeded):
    await _add_environment(
        db,
        seeded.tenant_a_id,
        slug="nogw",
        token="lab-token",
        gateway_internal_url=None,
    )
    url, _token, _proxy, forward_host = await _load_credentials(
        db, seeded.tenant_a_id, "nogw"
    )
    assert url == _LAB_FLAT_URL
    assert forward_host is None


def test_build_client_forward_host_via_proxy():
    client = _build_http_client(
        _GATEWAY,
        "t",
        proxy_url="http://10.200.0.2:8888",
        forward_host="svc-nautobot.base.test",
    )
    # The edge proxy strips Host as hop-by-hop and re-applies this header.
    assert client.headers["X-Daalu-Forward-Host"] == "svc-nautobot.base.test"
    assert "Host" not in {k.title() for k in client.headers}


def test_build_client_forward_host_direct():
    client = _build_http_client(
        _GATEWAY, "t", forward_host="svc-nautobot.base.test"
    )
    assert client.headers["Host"] == "svc-nautobot.base.test"
    assert "X-Daalu-Forward-Host" not in client.headers
