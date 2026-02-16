# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/config/loader.py

import logging
import os
import yaml
from pathlib import Path
from .models import ClusterConfig
from .models import DaaluConfig

log = logging.getLogger("daalu")


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge *override* into *base* (mutates base).
    Only overwrites when the override value is non-empty.
    """
    for key, value in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(base[key], value)
        else:
            if value not in (None, ""):
                base[key] = value
    return base


def _find_secrets_file(config_path: Path) -> Path | None:
    """
    Locate secrets.yaml using this priority:

    1. DAALU_SECRETS_FILE environment variable (explicit override)
    2. cloud-config/secrets.yaml relative to workspace root
    3. secrets.yaml in the same directory as the cluster config
    """
    env = os.environ.get("DAALU_SECRETS_FILE")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        log.warning("DAALU_SECRETS_FILE=%s does not exist — skipping", env)
        return None

    workspace = os.environ.get("WORKSPACE_ROOT")
    if workspace:
        p = Path(workspace) / "cloud-config" / "secrets.yaml"
        if p.is_file():
            return p

    p = config_path.parent / "secrets.yaml"
    if p.is_file():
        return p

    return None


def _load_yaml(path: Path) -> dict:
    """Load a YAML file, expanding ${ENV_VAR} references."""
    raw = path.read_text()
    expanded = os.path.expandvars(raw)
    return yaml.safe_load(expanded) or {}


def load_config(path: str | Path) -> DaaluConfig:
    """
    Load and validate a Daalu YAML config.

    Secrets are injected via two methods (both can be used together):

    **Method 1 — secrets.yaml file (recommended)**
        Place a ``cloud-config/secrets.yaml`` whose structure mirrors the
        cluster config.  The loader discovers it automatically and deep-merges
        it into the config dict before Pydantic validation.  Discovery order:
          1. ``DAALU_SECRETS_FILE`` env var → explicit path
          2. ``$WORKSPACE_ROOT/cloud-config/secrets.yaml``
          3. ``secrets.yaml`` next to the cluster config file

    **Method 2 — environment variables**
        Use ``${ENV_VAR}`` placeholders directly inside cluster.yaml (or
        secrets.yaml).  ``os.path.expandvars`` resolves them at load time.
    """
    path = Path(path)
    data = _load_yaml(path)

    secrets_path = _find_secrets_file(path)
    if secrets_path:
        log.debug("Merging secrets from %s", secrets_path)
        secrets = _load_yaml(secrets_path)
        _deep_merge(data, secrets)
    else:
        log.debug("No secrets.yaml found — proceeding without secrets merge")

    return DaaluConfig.model_validate(data)