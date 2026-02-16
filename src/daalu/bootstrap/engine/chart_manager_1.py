# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu.bootstrap.engine/chart_manager.py

from pathlib import Path
from daalu.helm.charts import ensure_chart


def prepare_chart(*, ssh, component) -> Path:
    """
    Ensure Helm chart exists on the controller node and return its remote path.
    """

    # 1. Ensure chart exists locally
    local_chart = ensure_chart(
        repo_name=component.repo_name,
        repo_url=component.repo_url,
        chart=component.chart,
        version=component.version,
        target_dir=component.local_chart_dir,
        local_chart_dir=component.local_chart_dir,
    )


    # 2. Define remote path
    remote_chart = component.remote_chart_dir / component.chart

    # 3. Ensure parent dir exists
    ssh.run(f"mkdir -p {component.remote_chart_dir}", sudo=True)

    # 4. Upload chart directory
    ssh.put_dir(
        local_dir=local_chart,
        remote_dir=remote_chart,
        sudo=True,
    )

    # 5. Return REMOTE path (this is what Helm uses)
    return remote_chart
