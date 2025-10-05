# src/daalu/config/loader.py

import yaml
from .models import ClusterConfig
from pathlib import Path

def load_config(path: str | Path) -> ClusterConfig:
    data = yaml.safe_load(Path(path).read_text())
    return ClusterConfig.model_validate(data)
