# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

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
import logging

log = logging.getLogger("daalu")


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

    def _run(self, *, cli, cmd: str, hostname: str, env=None, sudo=True):
        log_file = self._log_file
        start_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        prefix = ""
        if env:
            prefix = " ".join(f"{k}={v}" for k, v in env.items()) + " "

        shell_cmd = f"{prefix}{cmd}"
        final = f"bash -lc {shell_cmd!r}"
        if sudo:
            final = f"sudo -S {final}"

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n[{start_ts}] ({hostname}) $ {final}\n")

        rc, out, err = cli.run(final, sudo=False)

        with open(log_file, "a", encoding="utf-8") as f:
            if out:
                f.write(f"({hostname}) [stdout]\n{out}\n")
            if err:
                f.write(f"({hostname}) [stderr]\n{err}\n")
            f.write(f"({hostname}) [exit {rc}]\n")

        return rc, out, err


    def _shq(self, s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"



    def _log(self, message: str) -> None:
        ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{ts}] {message}"
        log.debug(line, flush=True)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _ctx(self) -> dict:
        """
        Common event context for CSI events.
        Mirrors CephManager semantics.
        """
        return dict(self.run_ctx)
