# src/daalu/helm/charts.py

from pathlib import Path
import subprocess

def ensure_chart(*, repo_name: str, chart: str, version: str | None, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    chart_dir = target_dir / chart

    if chart_dir.exists():
        return chart_dir

    cmd = [
        "helm",
        "pull",
        f"{repo_name}/{chart}",
        "--untar",
        "--untardir",
        str(target_dir),
    ]

    if version:
        cmd.extend(["--version", version])

    subprocess.run(cmd, check=True)

    return chart_dir
