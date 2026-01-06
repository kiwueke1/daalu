from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Optional, Sequence, Union
from pathlib import Path

Cmd = Sequence[Union[str, "os.PathLike[str]"]]


@dataclass
class CommandRunner:
    logger: Optional[object] = None
    dry_run: bool = False
    label: Optional[str] = None

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
        label = self.label or "cmd"
        cmd_str = " ".join(map(str, cmd))

        # --- Log command ---
        if self.logger:
            self.logger.log(f"[{label}] $ {cmd_str}")

        if self.dry_run:
            if self.logger:
                self.logger.log(f"[{label}] dry-run: skipped execution")
            return subprocess.CompletedProcess(
                args=list(cmd),
                returncode=0,
                stdout="",
                stderr="",
            )

        start = time.time()

        try:
            result = subprocess.run(
                list(cmd),
                capture_output=True,
                check=check,
                text=text,
                cwd=cwd,
                env=env,
            )

        except subprocess.CalledProcessError as e:
            if self.logger:
                self.logger.log(f"[{label}][exit {e.returncode}]")
                if e.stdout:
                    self.logger.log(f"[{label}][stdout]\n{e.stdout.rstrip()}")
                if e.stderr:
                    self.logger.log(f"[{label}][stderr]\n{e.stderr.rstrip()}")
            raise

        duration = time.time() - start

        # --- Log outputs ---
        if self.logger:
            if result.stdout:
                self.logger.log(f"[{label}][stdout]\n{result.stdout.rstrip()}")
            if result.stderr:
                self.logger.log(f"[{label}][stderr]\n{result.stderr.rstrip()}")
            self.logger.log(f"[{label}][exit {result.returncode}] ({duration:.2f}s)")

        return result
