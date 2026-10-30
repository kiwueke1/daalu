"""JWKS source must follow the *target cluster*, not a global setting.

Regression cover for the NV-CM tool-SSO break: every environment was handed
``settings.keycloak_internal_issuer_url`` as its JWKS source, including
environments installed into a customer cluster over WireGuard. Those clusters
run no Keycloak, so the in-cluster Service name does not resolve there.

The failure is silent rather than loud, which is what made it expensive to
find: Nautobot's ``nv_config_manager_auth`` plugin swallows the JWKS fetch
error at ``log.debug``, and its ``JWTCookieMiddleware`` only considers
providers with ``user_provider: true``. So a bad JWKS URL does not 401 — it
drops the browser to Nautobot's *own* login page while machine ``svc-*`` calls
keep working (Envoy fetches JWKS on its own path). Hence these assertions test
the rendered value directly.
"""

from __future__ import annotations

import uuid

import pytest

from daalu_automation.config_manager_controller import values

_EXTERNAL = "https://auth.daalu.io/realms/daalu"
_INTERNAL = "http://keycloak.daalu-automation.svc.cluster.local:8080/realms/daalu"


class _Settings:
    """Minimal stand-in for the fields render_values reads."""

    is_production = False
    keycloak_issuer_url = _EXTERNAL
    keycloak_internal_issuer_url = _INTERNAL
    keycloak_token_audience = "nv-config-manager"
    keycloak_ui_client_id = "nv-config-manager-ui"
    keycloak_nvcm_superuser_roles = "nvcm-superuser"
    config_manager_harbor_registry = ""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Row:
    """Duck-typed ConfigManagerTenant — render_values only reads attributes."""

    def __init__(self, *, tunnel_id):
        self.base_hostname = "default.cmtools.daalu.io"
        self.namespace = "cm-default"
        self.components = {"nautobot": True, "render": True}
        self.size_profile = "small"
        self.target_cluster_tunnel_id = tunnel_id


@pytest.fixture
def settings(monkeypatch):
    s = _Settings()
    monkeypatch.setattr(values, "get_settings", lambda: s)
    return s


def _oidc(tunnel_id):
    return values.render_values(_Row(tunnel_id=tunnel_id))["oidc"]


def test_operator_cluster_uses_internal_jwks(settings):
    """NULL tunnel → installs here, where the external issuer is split-horizon."""
    oidc = _oidc(None)
    assert oidc["internalIssuerUrl"] == _INTERNAL
    assert oidc["jwksUri"] == _INTERNAL + "/protocol/openid-connect/certs"


def test_customer_cluster_uses_external_jwks(settings):
    """NOT NULL tunnel → a cluster with no Keycloak; the internal name is unresolvable."""
    oidc = _oidc(uuid.uuid4())
    assert oidc["internalIssuerUrl"] is None
    assert oidc["jwksUri"] == _EXTERNAL + "/protocol/openid-connect/certs"


def test_token_issuer_is_always_external(settings):
    """Only the *key fetch* varies by cluster; the token ``iss`` must not.

    The JWTs are minted by the public Keycloak, so rewriting issuerUrl to the
    internal Service URL would make every token fail issuer validation.
    """
    assert _oidc(None)["issuerUrl"] == _EXTERNAL
    assert _oidc(uuid.uuid4())["issuerUrl"] == _EXTERNAL


def test_jwks_uri_always_emitted(settings):
    """Never leave jwksUri unset: the chart helpers prefer it over
    internalIssuerUrl, and the upstream Deployer fills in its own value if we
    don't, silently taking the choice back."""
    for tunnel_id in (None, uuid.uuid4()):
        assert _oidc(tunnel_id)["jwksUri"]


def test_no_internal_issuer_configured_falls_back_to_external(settings):
    """An operator cluster with the setting unset must still get a usable URL."""
    settings.keycloak_internal_issuer_url = ""
    oidc = _oidc(None)
    assert oidc["internalIssuerUrl"] is None
    assert oidc["jwksUri"] == _EXTERNAL + "/protocol/openid-connect/certs"
