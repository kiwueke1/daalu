# daalu/execution/runner.py
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence, Union

Cmd = Sequence[Union[str, "os.PathLike[str]"]]

@dataclass
class CommandRunner:
    logger: Optional[object] = None
    dry_run: bool = False

    def run(
        self,
        cmd: Cmd,
        *,
        capture_output: bool = False,
        check: bool = False,
        text: bool = True,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        # Log command
        if self.logger:
            try:
                self.logger.info("RUN: %s", " ".join(map(str, cmd)))
            except Exception:
                pass

        if self.dry_run:
            # Return a CompletedProcess-like object
            return subprocess.CompletedProcess(args=list(cmd), returncode=0, stdout="", stderr="")

        return subprocess.run(
            list(cmd),
            capture_output=capture_output,
            check=check,
            text=text,
            cwd=cwd,
            env=env,
        )
