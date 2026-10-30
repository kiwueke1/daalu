"""Smoke tests for the vendored ``nv_config_manager_installer`` package.

Guards the import chain Daalu's controller drives: if a re-vendor drops a
module or introduces a dep we don't ship, these fail fast rather than at
provision time. We import the ``Deployer`` code path only (not ``tui/``,
which is intentionally not vendored).
"""

from __future__ import annotations

import sys

import pytest

# The vendored upstream package uses ``enum.StrEnum`` (Python 3.11+), matching
# the controller's runtime image (python:3.11) and CI (3.11). Skip on older
# interpreters so a 3.10 dev machine doesn't fail at collection.
pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="vendored nv_config_manager_installer requires Python 3.11+ (StrEnum)",
)


def test_deployer_import_chain_loads() -> None:
    """The Deployer + its transitive imports load against Daalu's deps."""
    from nv_config_manager_installer.deployer import (
        DeployCallback,
        Deployer,
        DeployOptions,
    )

    assert Deployer is not None
    assert DeployOptions is not None
    assert DeployCallback is not None


def test_install_config_schema_loads_and_defaults() -> None:
    """The Pydantic config model validates and defaults to kubernetes secrets."""
    from nv_config_manager_installer.schema import (
        NVConfigManagerInstallConfig,
        SecretsMethod,
    )

    cfg = NVConfigManagerInstallConfig()
    # Daalu always installs with in-cluster (kubernetes) secrets, never ESO.
    assert cfg.secrets.method == SecretsMethod.KUBERNETES
    # A minimal valid config (hostname is the one deploy-time-required field).
    cfg.cluster.hostname = "acme.cm.daalu.io"
    assert cfg.cluster.namespace  # has a default


def test_tui_is_not_vendored() -> None:
    """The Textual TUI is intentionally excluded from the vendored copy."""
    import importlib.util

    assert importlib.util.find_spec("nv_config_manager_installer.tui") is None
