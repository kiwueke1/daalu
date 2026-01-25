# src/daalu/bootstrap/infrastructure/utils/assets.py

from __future__ import annotations
from pathlib import Path


def infra_asset_path(
    workspace_root: Path,
    component: str,
    filename: str = "config.yaml",
) -> Path:
    return (
        workspace_root
        / "assets"
        / component
        / filename
    )
