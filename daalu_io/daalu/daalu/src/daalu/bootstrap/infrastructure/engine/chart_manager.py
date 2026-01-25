# src/daalu/bootstrap/infrastructure/engine/chart_manager.py

from pathlib import Path
from daalu.helm.charts import ensure_chart


def prepare_chart(*, ssh, component) -> Path:
    """
    Ensure Helm chart exists on the controller node and return its remote path.
    Supports:
      - Remote Helm charts
      - Local vendored charts
      - Monorepo subcharts (Istio)
    """

    # ------------------------------------------------------------
    # Case 1: Local vendored charts (Istio-style)
    # ------------------------------------------------------------
    if component.local_chart_dir is not None:
        # Upload ENTIRE repo root
        ssh.run(f"mkdir -p {component.remote_chart_dir}", sudo=True)

        ssh.put_dir(
            local_dir=component.local_chart_dir,
            remote_dir=component.remote_chart_dir,
            sudo=True,
        )

        # Helm installs subchart inside repo
        return component.remote_chart_dir / component.chart

    # ------------------------------------------------------------
    # Case 2: Standard Helm repo charts
    # ------------------------------------------------------------
    local_chart = ensure_chart(
        repo_name=component.repo_name,
        repo_url=component.repo_url,
        chart=component.chart,
        version=component.version,
        target_dir=component.local_chart_dir,
        local_chart_dir=component.local_chart_dir,
    )

    remote_chart = component.remote_chart_dir / component.chart

    ssh.run(f"mkdir -p {component.remote_chart_dir}", sudo=True)

    ssh.put_dir(
        local_dir=local_chart,
        remote_dir=remote_chart,
        sudo=True,
    )

    return remote_chart
