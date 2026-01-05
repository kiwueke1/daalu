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
    