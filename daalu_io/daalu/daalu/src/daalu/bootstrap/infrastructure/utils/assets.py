# src/daalu/bootstrap/infrastructure/utils/assets.py

from pathlib import Path
from typing import Optional


daalu_artifacts = Path("~/.daalu").expanduser()

def infra_asset_path(
    daalu_assets: Path,
    component: str,
    filename: Optional[str] = None,
) -> Path:
    base = daalu_assets / "assets" / component
    return base / filename if filename else base
