# src/daalu/config/loader.py

import os
import yaml
from pathlib import Path
from .models import ClusterConfig

def load_config(path: str | Path) -> ClusterConfig:
    raw = Path(path).read_text()

    # expand environment variables like ${WORKSPACE_ROOT}
    expanded = os.path.expandvars(raw)

    data = yaml.safe_load(expanded)
    return ClusterConfig.model_validate(data)
