# src/daalu/utils/shell.py

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Sequence

import paramiko
import subprocess

def run_remote_logged(
    *,
    cli: paramiko.SSHClient,
    cmd: str,
    log_file: Path,
    hostname: str,
    env: Optional[dict] = None,
    sudo: bool = True,
    timeout: int = 300,
) -> Tuple[int, str, str]:
    """
    Execute a shell command on a remote host via SSH with full logging.

    - Writes command + stdout/stderr to the provided log_file
    - Tags output with hostname
    - Returns (rc, stdout, stderr)
    """

    def shq(v: str) -> str:
        return "'" + v.replace("'", "'\"'\"'") + "'"

    # Build command
    prefix = ""
    if env:
        exports = " ".join(f"{k}={shq(str(v))}" for k, v in env.items())
        prefix = f"{exports} "

    shell_cmd = f"{prefix}{cmd}"
    final_cmd = (
        f"sudo -S bash -lc {shq(shell_cmd)}"
        if sudo
        else f"bash -lc {shq(shell_cmd)}"
    )

    start_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\n[{start_ts}] ({hostname}) $ {final_cmd}\n")

    stdin, stdout, stderr = cli.exec_command(final_cmd, timeout=timeout)

    out_chunks: list[str] = []
    err_chunks: list[str] = []

    while not stdout.channel.exit_status_ready():
        if stdout.channel.recv_ready():
            chunk = stdout.channel.recv(1024).decode("utf-8", "replace")
            out_chunks.append(chunk)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"({hostname}) [stdout] {chunk}")
        if stdout.channel.recv_stderr_ready():
            chunk = stdout.channel.recv_stderr(1024).decode("utf-8", "replace")
            err_chunks.append(chunk)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"({hostname}) [stderr] {chunk}")
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
            f.write(f"({hostname}) [stdout]\n{out_rem}\n")
        if err_rem.strip():
            f.write(f"({hostname}) [stderr]\n{err_rem}\n")
        f.write(f"({hostname}) [exit {rc}]\n")

    return rc, "".join(out_chunks), "".join(err_chunks)


def run_logged(
    cmd: Sequence[str],
    *,
    logger,
    label: str,
    env: Optional[dict] = None,
    cwd: Optional[Path] = None,
    timeout: int = 900,
) -> None:
    """
    Execute a local command with full structured logging.

    - Streams stdout/stderr into RunLogger
    - Raises RuntimeError on non-zero exit
    """

    logger.log(f"[{label}] $ {' '.join(cmd)}")

    start = time.time()

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
    )

    assert proc.stdout and proc.stderr

    # Stream output live
    for line in proc.stdout:
        logger.log(f"[{label}] {line.rstrip()}")

    for line in proc.stderr:
        logger.log(f"[{label}][stderr] {line.rstrip()}")

    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(f"[{label}] command timed out after {timeout}s")

    elapsed = round(time.time() - start, 2)

    if rc != 0:
        raise RuntimeError(f"[{label}] failed (rc={rc}) after {elapsed}s")

    logger.log(f"[{label}] completed successfully in {elapsed}s")
