# src/daalu/config/loader.py

import os
import yaml
from pathlib import Path
from .models import ClusterConfig
from .models import DaaluConfig


def load_config_1(path: str | Path) -> ClusterConfig:
    raw = Path(path).read_text()

    # expand environment variables like ${WORKSPACE_ROOT}
    expanded = os.path.expandvars(raw)

    data = yaml.safe_load(expanded)
    return ClusterConfig.model_validate(data)

def load_config(path: str | Path) -> DaaluConfig:
    """
    Load and validate a Daalu YAML config.
    - Expands environment variables (${WORKSPACE_ROOT}, etc.)
    - Validates strictly against DaaluConfig
    """
    path = Path(path)

    raw = path.read_text()

    # Expand environment variables like ${WORKSPACE_ROOT}
    expanded = os.path.expandvars(raw)

    data = yaml.safe_load(expanded) or {}

    return DaaluConfig.model_validate(data)