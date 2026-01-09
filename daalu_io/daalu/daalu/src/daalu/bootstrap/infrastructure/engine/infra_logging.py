# src/daalu/bootstrap/infrastructure/engine/infra_logging.py

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Any

from daalu.utils.ssh_runner import SSHRunner


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class InfraLogContext:
    run_id: str
    component: str = "unknown"
    stage: str = "unknown"
    host: str = "unknown"


class InfraJsonlLogger:
    """
    Writes structured logs (JSONL) for an infrastructure deployment run.
    One JSON object per line.
    """

    def __init__(self, *, log_dir: Path | None = None, run_id: str | None = None):
        self.run_id = run_id or f"infra-{_utc_compact()}-{uuid.uuid4().hex[:8]}"
        self.log_dir = log_dir or (Path.home() / ".daalu" / "logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / f"{self.run_id}.jsonl"

        # Initialize file with a header event
        self._write(
            {
                "ts": _utc_ts(),
                "event": "infra.run.start",
                "run_id": self.run_id,
                "log_path": str(self.path),
            }
        )

        self.ctx = InfraLogContext(run_id=self.run_id)

    def set_component(self, component: str) -> None:
        self.ctx.component = component

    def set_stage(self, stage: str) -> None:
        self.ctx.stage = stage

    def set_host(self, host: str) -> None:
        self.ctx.host = host

    def log_event(self, event: str, **fields: Any) -> None:
        payload = {
            "ts": _utc_ts(),
            "event": event,
            "run_id": self.ctx.run_id,
            "component": self.ctx.component,
            "stage": self.ctx.stage,
            "host": self.ctx.host,
            **fields,
        }
        self._write(payload)

    def log_command(
        self,
        *,
        cmd: str,
        sudo: bool,
        rc: int,
        stdout: str,
        stderr: str,
        duration_ms: int,
    ) -> None:
        self.log_event(
            "infra.command",
            cmd=cmd,
            sudo=sudo,
            rc=rc,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
        )

    def _write(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


class LoggedSSHRunner(SSHRunner):
    def __init__(self, inner: SSHRunner, logger: InfraJsonlLogger, *, host_label: str | None = None):
        self._inner = inner
        self._logger = logger
        if host_label:
            self._logger.set_host(host_label)

    def run(self, cmd: str, sudo: bool = False):
        start = time.time()
        rc, out, err = self._inner.run(cmd, sudo=sudo)
        dur_ms = int((time.time() - start) * 1000)

        self._logger.log_command(
            cmd=cmd,
            sudo=sudo,
            rc=rc,
            stdout=out or "",
            stderr=err or "",
            duration_ms=dur_ms,
        )
        return rc, out, err

    def put_text(self, content: str, remote_path, sudo: bool = False):
        self._logger.log_event(
            "infra.put_text",
            remote_path=str(remote_path),
            sudo=sudo,
            bytes=len(content.encode("utf-8", "replace")),
        )
        return self._inner.put_text(content, remote_path, sudo=sudo)

    def put_file(self, local_path, remote_path, sudo: bool = False):
        lp = Path(local_path)
        size = lp.stat().st_size if lp.exists() else None
        self._logger.log_event(
            "infra.put_file",
            local_path=str(local_path),
            remote_path=str(remote_path),
            sudo=sudo,
            bytes=size,
        )
        return self._inner.put_file(local_path, remote_path, sudo=sudo)

    def __getattr__(self, name):
        return getattr(self._inner, name)


    # Expose inner for edge cases
    @property
    def inner(self) -> SSHRunner:
        return self._inner

    @property
    def logger(self) -> InfraJsonlLogger:
        return self._logger
