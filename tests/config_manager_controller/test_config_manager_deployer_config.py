"""Tests for the row → NVConfigManagerInstallConfig mapper (deployer_config)."""

from __future__ import annotations

import sys
import uuid

import pytest

# deployer_config imports the vendored nv_config_manager_installer schema, which
# uses enum.StrEnum (Python 3.11+) — matching the runtime image and CI. Skip the
# whole module on 3.10 dev machines *before* the heavy imports below, so
# collection doesn't fail (a plain skipif mark wouldn't stop module execution).
if sys.version_info < (3, 11):  # pragma: no cover
    pytest.skip(
        "deployer_config imports nv_config_manager_installer (Python 3.11+)",
        allow_module_level=True,
    )

from daalu_automation.config_manager_controller import deployer_config as dc
from daalu_automation.models import ConfigManagerTenant
from nv_config_manager_installer.schema import (
    NV_CONFIG_MANAGER_IMAGE_KEYS,
    DeploySize,
    ImageSource,
    LBProvider,
    NVConfigManagerInstallConfig,
    SecretsMethod,
    SSOProvider,
)


def _row(**kw) -> ConfigManagerTenant:
    defaults = dict(
        tenant_id=uuid.UUID("11111111111111111111111111111111"),
        namespace="cm-acme",
        base_hostname="acme.cm.daalu.io",
        components={},
        size_profile="small",
        chart_version="",
    )
    defaults.update(kw)
    return ConfigManagerTenant(**defaults)


class _Settings:
    """Minimal stand-in for the fields deployer_config reads."""

    is_production = False
    config_manager_harbor_registry = "harbor.daalu.io"
    config_manager_image_pull_secret = ""
    keycloak_issuer_url = "https://auth.daalu.io/realms/daalu"
    keycloak_token_audience = "daalu-hub"
    keycloak_ui_client_id = "nv-config-manager-ui"
    keycloak_internal_issuer_url = ""
    keycloak_ui_client_secret = ""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_release_name_is_stable_and_namespace_independent() -> None:
    row = _row(namespace="cm-something-else")
    assert dc.release_name(row) == "cm-111111111111"


def test_cluster_fields() -> None:
    cfg = dc.build_install_config(_row(size_profile="medium"), settings=_Settings())
    assert cfg.cluster.hostname == "acme.cm.daalu.io"
    assert cfg.cluster.namespace == "cm-acme"
    assert cfg.cluster.release_name == "cm-111111111111"
    assert cfg.cluster.size == DeploySize.MEDIUM
    # Subcharts are vendored — Deployer must skip ``helm dependency update``.
    assert cfg.cluster.airgapped is True
    assert cfg.cluster.environment == "staging"


def test_environment_production() -> None:
    cfg = dc.build_install_config(_row(), settings=_Settings(is_production=True))
    assert cfg.cluster.environment == "production"


def test_invalid_size_falls_back_to_small() -> None:
    cfg = dc.build_install_config(_row(size_profile="x:bogus"), settings=_Settings())
    assert cfg.cluster.size == DeploySize.SMALL


def test_secrets_always_kubernetes() -> None:
    cfg = dc.build_install_config(_row(), settings=_Settings())
    assert cfg.secrets.method == SecretsMethod.KUBERNETES


def test_service_toggles_from_components_and_ui_is_dropped() -> None:
    row = _row(
        components={
            "render": True,
            "configStore": True,
            "temporal": False,
            "nautobot": True,
            "ztp": True,
            "dhcp": False,
            "ui": True,  # no upstream equivalent — must be ignored, not crash
        }
    )
    cfg = dc.build_install_config(row, settings=_Settings())
    assert cfg.services.render is True
    assert cfg.services.config_store is True
    assert cfg.services.temporal is False
    assert cfg.services.nautobot is True
    assert cfg.services.ztp is True
    assert cfg.services.dhcp is False
    # ServicesConfig has no ``ui`` attribute; the mapper must not set one.
    assert not hasattr(cfg.services, "ui")


def test_empty_components_uses_lean_defaults() -> None:
    cfg = dc.build_install_config(_row(components={}), settings=_Settings())
    # DEFAULT_COMPONENTS: render/configStore/temporal/nautobot on; ztp/dhcp off.
    assert cfg.services.render is True
    assert cfg.services.config_store is True
    assert cfg.services.temporal is True
    assert cfg.services.nautobot is True
    assert cfg.services.ztp is False
    assert cfg.services.dhcp is False


def test_harbor_image_overrides_cover_the_six_app_keys() -> None:
    cfg = dc.build_install_config(_row(), settings=_Settings())
    assert cfg.images.source == ImageSource.REGISTRY
    expected_keys = {key for key, _ in NV_CONFIG_MANAGER_IMAGE_KEYS}
    assert set(cfg.images.overrides) == expected_keys
    assert (
        cfg.images.overrides["nvConfigManager"].repository
        == "harbor.daalu.io/nv-config-manager/nv-config-manager"
    )
    # Tag left empty → inherits the chart default.
    assert cfg.images.overrides["nvConfigManager"].tag == ""


def test_no_image_overrides_when_harbor_unset() -> None:
    cfg = dc.build_install_config(
        _row(), settings=_Settings(config_manager_harbor_registry="")
    )
    assert cfg.images.overrides == {}


def test_no_pull_secret_by_default() -> None:
    cfg = dc.build_install_config(_row(), settings=_Settings())
    assert cfg.images.pull_secret.name == ""
    assert cfg.images.pull_secret.password == ""


def test_pull_secret_when_configured() -> None:
    cfg = dc.build_install_config(
        _row(), settings=_Settings(config_manager_image_pull_secret="regcred-harbor")
    )
    assert cfg.images.pull_secret.name == "regcred-harbor"


def test_oidc_enabled_bearer_only() -> None:
    cfg = dc.build_install_config(_row(), settings=_Settings())
    assert cfg.sso.enabled is True
    assert cfg.sso.provider == SSOProvider.KEYCLOAK
    assert cfg.sso.issuer_url == "https://auth.daalu.io/realms/daalu"
    assert cfg.sso.audiences == "daalu-hub"
    # The hub validates bearer service JWTs (audiences above), but the chart's
    # gateway SecurityPolicy requires a non-empty oidc.clientId to install, so
    # the UI client id is always populated. No client secret is wired here.
    assert cfg.sso.client_id == "nv-config-manager-ui"
    assert cfg.sso.client_secret == ""


def test_oidc_client_id_overridable() -> None:
    cfg = dc.build_install_config(
        _row(), settings=_Settings(keycloak_ui_client_id="custom-ui-client")
    )
    assert cfg.sso.client_id == "custom-ui-client"


def test_internal_issuer_unset_by_default() -> None:
    cfg = dc.build_install_config(_row(), settings=_Settings())
    assert cfg.sso.internal_issuer == ""


def test_ui_client_secret_carried_when_set() -> None:
    cfg = dc.build_install_config(
        _row(), settings=_Settings(keycloak_ui_client_secret="ui-secret-xyz")
    )
    assert cfg.sso.client_secret == "ui-secret-xyz"


def test_ui_client_secret_empty_by_default() -> None:
    cfg = dc.build_install_config(_row(), settings=_Settings())
    assert cfg.sso.client_secret == ""


def test_internal_issuer_set_for_envoy_jwks() -> None:
    url = "http://keycloak.daalu-automation.svc.cluster.local:8080/realms/daalu"
    cfg = dc.build_install_config(
        _row(), settings=_Settings(keycloak_internal_issuer_url=url)
    )
    # iss check stays external; only the JWKS fetch uses the internal URL.
    assert cfg.sso.issuer_url == "https://auth.daalu.io/realms/daalu"
    assert cfg.sso.internal_issuer == url


def test_internal_issuer_dropped_for_customer_cluster() -> None:
    """A customer cluster over WireGuard runs no Keycloak of its own.

    Handing it the operator cluster's in-cluster Service URL makes every JWKS
    fetch fail DNS, which the Nautobot auth plugin swallows silently — the
    browser lands on Nautobot's own login page instead of being SSO'd. Must
    fall back to the public issuer. Mirrors values.render_values.
    """
    url = "http://keycloak.daalu-automation.svc.cluster.local:8080/realms/daalu"
    cfg = dc.build_install_config(
        _row(target_cluster_tunnel_id=uuid.uuid4()),
        settings=_Settings(keycloak_internal_issuer_url=url),
    )
    assert cfg.sso.internal_issuer == ""
    assert cfg.sso.issuer_url == "https://auth.daalu.io/realms/daalu"


def test_oidc_disabled_when_no_issuer() -> None:
    cfg = dc.build_install_config(_row(), settings=_Settings(keycloak_issuer_url=""))
    assert cfg.sso.enabled is False


def test_infrastructure_tls_on_and_no_loadbalancer() -> None:
    cfg = dc.build_install_config(_row(), settings=_Settings())
    assert cfg.infrastructure.tls is True
    # Daalu fronts the shared gateway via traefik — no per-tenant LB.
    assert cfg.infrastructure.load_balancer.provider == LBProvider.NONE


def test_config_is_pydantic_valid_end_to_end() -> None:
    """The produced config must pass model validation (hostname set, etc.)."""
    cfg = dc.build_install_config(_row(), settings=_Settings())
    NVConfigManagerInstallConfig.model_validate(cfg.model_dump())
