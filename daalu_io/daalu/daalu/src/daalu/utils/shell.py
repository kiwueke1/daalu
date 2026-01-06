# src/daalu/utils/shell.py
from __future__ import annotations

import subprocess
from typing import Sequence, Optional
from pathlib import Path

from daalu.utils.logging import RunLogger
import subprocess
from typing import Optional
from pathlib import Path
from daalu.utils.execution import ExecutionContext

def run(
    cmd: list[str],
    *,
    check: bool = True,
    ctx: Optional[ExecutionContext] = None,
) -> None:
    if ctx and ctx.dry_run:
        print(f"[dry-run] {' '.join(cmd)}")
        return
    
    subprocess.run(cmd, check=check)
    



def run_logged(
    cmd: Sequence[str],
    *,
    logger: RunLogger,
    label: str,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
) -> None:
    """
    Execute a shell command with full stdout/stderr logging.
    """
    logger.log(f"[{label}] $ {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

        if result.stdout:
            logger.log(f"[{label}][stdout]\n{result.stdout.rstrip()}")
        if result.stderr:
            logger.log(f"[{label}][stderr]\n{result.stderr.rstrip()}")

    except subprocess.CalledProcessError as e:
        logger.log(f"[{label}][exit {e.returncode}]")
        if e.stdout:
            logger.log(e.stdout.rstrip())
        if e.stderr:
            logger.log(e.stderr.rstrip())
        raise
