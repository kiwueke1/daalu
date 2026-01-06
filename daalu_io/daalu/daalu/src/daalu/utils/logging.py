# src/daalu/utils/logging.py

# daalu/utils/logging.py
from __future__ import annotations

from pathlib import Path
from datetime import datetime
from typing import Optional
import sys


class RunLogger:
    """
    Central run-scoped logger used by Ceph, Metal3, SSH, Helm, etc.
    """

    def __init__(
        self,
        name: str,
        *,
        base_dir: Optional[Path] = None,
        echo: bool = True,
    ):
        self.name = name
        self.echo = echo

        base = base_dir or (Path.home() / ".daalu" / "logs")
        base.mkdir(parents=True, exist_ok=True)

        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        self.log_file = base / f"{name}-{ts}.log"

        self.log_file.write_text(
            f"# {name} deployment log started {ts} UTC\n\n",
            encoding="utf-8",
        )

    def log(self, msg: str):
        ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{ts}] {msg}"

        if self.echo:
            print(line, file=sys.stdout, flush=True)

        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
