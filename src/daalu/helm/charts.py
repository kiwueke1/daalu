# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/helm/charts.py

import subprocess
from pathlib import Path
from daalu.utils.ssh_runner import SSHRunner


def ensure_chart_remote(
    *,
    ssh: SSHRunner,
    repo_name: str,
    repo_url: str,
    chart: str,
    version: str | None,
    target_dir: Path,
) -> Path:
    """
    Ensure Helm chart exists on the REMOTE host under target_dir.
    """

    chart_dir = target_dir / chart

    # 1. Create base dir
    ssh.run(
        f"mkdir -p {target_dir}",
        #check=True,
    )

    # 2. Short-circuit if chart already exists
    rc, _, _ = ssh.run(
        f"test -d {chart_dir}",
        #check=False,
    )
    if rc == 0:
        return chart_dir

    # 3. Add repo (idempotent)
    ssh.run(
        f"helm repo add {repo_name} {repo_url} || true",
        #check=True,
    )

    # 4. Update repos
    ssh.run(
        "helm repo update",
        #check=True,
    )

    # 5. Pull chart
    cmd = (
        f"helm pull {repo_name}/{chart} "
        f"--untar --untardir {target_dir}"
    )
    if version:
        cmd += f" --version {version}"

    ssh.run(cmd)

    return chart_dir


def ensure_chart(
    *,
    repo_name: str | None,
    repo_url: str | None,
    chart: str,
    version: str | None,
    target_dir: Path,
    local_chart_dir: Path | None = None,
) -> Path:
    """
    Resolve a Helm chart either from:
    - a local vendored directory, OR
    - a remote Helm repo
    """

    # --------------------------------------------------
    # 1) Local chart mode (Istio, vendored charts)
    # --------------------------------------------------
    if local_chart_dir is not None:
        chart_dir = local_chart_dir / chart.split("/")[0]

        if not chart_dir.exists():
            raise FileNotFoundError(
                f"Local chart not found: {chart_dir}"
            )

        return chart_dir

    # --------------------------------------------------
    # 2) Repo-based chart mode
    # --------------------------------------------------
    if not repo_name or not repo_url:
        raise ValueError(
            f"repo_name/repo_url required for remote chart '{chart}'"
        )

    subprocess.run(
        ["helm", "repo", "add", repo_name, repo_url],
        check=False,
    )

    subprocess.run(["helm", "repo", "update"], check=True)

    subprocess.run(
        ["helm", "pull", f"{repo_name}/{chart}", "--untar", "--untardir", str(target_dir)],
        check=True,
    )

    return target_dir / chart


