# src/daalu/common/helpers.py

from __future__ import annotations
import subprocess
import time
from pathlib import Path
from typing import Optional

def run(
    cmd: list[str],
    *,
    env: Optional[dict] = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        env=env,
        capture_output=capture_output
    )

def kubectl(
    args: list[str],
    *,
    kubeconfig: Optional[Path] = None,
) -> None:
    env = None
    if kubeconfig:
        env = {"KUBECONFIG": str(kubeconfig)}
    run(["kubectl"] + args, env=env)

def wait_until(
    predicate,
    *,
    retries: int,
    delay: int,
    error: str,
):
    for _ in range(retries):
        if predicate():
            return
        time.sleep(delay)
    raise TimeoutError(error)

def clusterctl(args: list[str]) -> None:
    run(["clusterctl"], args)