# src/daalu/bootstrap/csi/base.py

from __future__ import annotations

import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

import paramiko

from daalu.observers.dispatcher import EventBus
from daalu.observers.events import new_ctx
from daalu.bootstrap.ceph.models import CephHost


class CSIBase:
    def __init__(
        self,
        *,
        bus: EventBus,
        ssh: paramiko.SSHClient,
        host: CephHost,
        env: str,
        context: str,
    ):
        self.bus = bus
        self.ssh = ssh
        self.host = host
        self.run_ctx = new_ctx(env=env, context=context)

        self._log_file = self._init_log_file()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _init_log_file(self) -> Path:
        """
        CSI logs live under ~/.daalu/logs/csi/
        """
        base = Path.home() / ".daalu" / "logs" / "csi"
        base.mkdir(parents=True, exist_ok=True)

        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = base / f"csi-deploy-{ts}.log"

        path.write_text(f"# CSI deployment log started {ts} UTC\n\n")
        return path

    # ------------------------------------------------------------------
    # Command runner (Ceph-compatible)
    # ------------------------------------------------------------------

    def _run(
        self,
        *,
        cli: paramiko.SSHClient,
        cmd: str,
        sudo: bool = True,
        hostname: Optional[str] = None,
        env: Optional[dict] = None,
    ) -> Tuple[int, str, str]:
        """
        Run a command over SSH with full logging.
        Signature intentionally matches CephManager._run().
        """

        host = hostname or self.host.hostname
        log_file = self._log_file

        prefix = ""
        if env:
            exports = " ".join(f"{k}={self._shq(v)}" for k, v in env.items())
            prefix = f"{exports} "

        shell_cmd = f"{prefix}{cmd}"
        final = f"sudo -S bash -lc {self._shq(shell_cmd)}" if sudo else f"bash -lc {self._shq(shell_cmd)}"

        start_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n[{start_ts}] ({host}) $ {final}\n")

        stdin, stdout, stderr = cli.exec_command(final)

        out_chunks, err_chunks = [], []

        while not stdout.channel.exit_status_ready():
            if stdout.channel.recv_ready():
                chunk = stdout.channel.recv(4096).decode("utf-8", "replace")
                out_chunks.append(chunk)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"({host}) [stdout] {chunk}")
            if stdout.channel.recv_stderr_ready():
                chunk = stdout.channel.recv_stderr(4096).decode("utf-8", "replace")
                err_chunks.append(chunk)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"({host}) [stderr] {chunk}")
            time.sleep(0.2)

        rc = stdout.channel.recv_exit_status()
        out_rem = stdout.read().decode("utf-8", "replace")
        err_rem = stderr.read().decode("utf-8", "replace")

        if out_rem:
            out_chunks.append(out_rem)
        if err_rem:
            err_chunks.append(err_rem)

        with open(log_file, "a", encoding="utf-8") as f:
            if out_rem.strip():
                f.write(f"({host}) [stdout]\n{out_rem}\n")
            if err_rem.strip():
                f.write(f"({host}) [stderr]\n{err_rem}\n")
            f.write(f"({host}) [exit {rc}]\n")

        return rc, "".join(out_chunks), "".join(err_chunks)

    def _shq(self, s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"



    def _log(self, message: str) -> None:
        ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{ts}] {message}"
        print(line, flush=True)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _ctx(self) -> dict:
        """
        Common event context for CSI events.
        Mirrors CephManager semantics.
        """
        return dict(self.run_ctx)
