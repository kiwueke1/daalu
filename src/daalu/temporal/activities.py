# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/temporal/activities.py
"""
Temporal activities for the daalu workflows.

Strategy: each activity runs `daalu` (or a related CLI command) as a subprocess,
streams stdout/stderr line-by-line, and emits each line as a heartbeat detail
so the workflow can mirror it into its progress query for the UI.

Why subprocess and not direct Python calls?
  - Preserves all CLI logic without refactor (SSH/helm state, signal handling)
  - Each stage is independent — no shared SSH client to thread through args
  - Output streaming matches what a CLI user already sees
  - Failure isolation is natural: re-run a single stage by re-running the activity
"""
from __future__ import annotations

import os
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import List, Optional

from temporalio import activity

from daalu.temporal.models import (
    CleanRequest,
    DeployRequest,
    MirrorImagesRequest,
    StageInput,
    StageResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _expand(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    return os.path.expanduser(os.path.expandvars(p))


def _run_streaming(
    *,
    cmd: List[str],
    cwd: str,
    env: Optional[dict] = None,
    log_tail_size: int = 8192,
    heartbeat_tail_lines: int = 200,
    heartbeat_interval_secs: float = 1.0,
) -> StageResult:
    """
    Run a subprocess and return a StageResult with exit code, duration, and
    the final tail of stdout+stderr.

    Heartbeat strategy
    ------------------
    Once per ``heartbeat_interval_secs`` (and on completion), we send one
    heartbeat carrying the latest tail::

        {"tail": ["line N-199", ..., "line N"], "n": N}

    The temporal-console UI reads this via Temporal's
    ``describe_workflow_execution`` (pending activities → heartbeat details)
    so the user sees a live tail of the running stage.

    We deliberately do NOT heartbeat per-line. Heartbeats are persisted and a
    busy stage emits tens of thousands of lines — per-line heartbeats blow up
    history. One heartbeat-per-second with a rolling tail is enough for a
    live-tail view and keeps history small.
    """
    started = time.monotonic()
    stage = activity.info().activity_type
    activity.logger.info("[%s] starting: %s", stage, " ".join(cmd))

    tail: deque[str] = deque(maxlen=heartbeat_tail_lines)
    last_heartbeat = 0.0

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env or os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assert proc.stdout is not None  # for type-checkers
    line_count = 0
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            line_count += 1
            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_interval_secs:
                activity.heartbeat({"tail": list(tail), "n": line_count})
                last_heartbeat = now
        # Final heartbeat with the tail at completion.
        activity.heartbeat({"tail": list(tail), "n": line_count})
    finally:
        proc.wait()

    duration = time.monotonic() - started
    log_tail = "\n".join(tail)[-log_tail_size:]

    success = proc.returncode == 0
    activity.logger.info(
        "[%s] finished rc=%d duration=%.1fs lines=%d",
        stage, proc.returncode, duration, line_count,
    )

    return StageResult(
        stage=stage,
        success=success,
        exit_code=proc.returncode,
        duration_secs=duration,
        log_tail=log_tail,
        error=None if success else f"exit code {proc.returncode}",
    )


def _deploy_command(stage: str, req: DeployRequest) -> List[str]:
    """Build a `daalu deploy --install <stage>` command from the request."""
    cmd: List[str] = ["daalu", "deploy", req.config_path, "--install", stage]

    if req.infra:
        cmd += ["--infra", req.infra]
    if req.local_registry:
        cmd += ["--local-registry"]
    if req.install_local_registry:
        cmd += ["--install-local-registry"]
    if req.registry_url:
        cmd += ["--registry-url", req.registry_url]
    if req.mgmt_kubeconfig:
        cmd += ["--mgmt-kubeconfig", _expand(req.mgmt_kubeconfig) or req.mgmt_kubeconfig]
    if req.cluster_kubeconfig:
        cmd += ["--cluster-kubeconfig", _expand(req.cluster_kubeconfig) or req.cluster_kubeconfig]
    if req.context:
        cmd += ["--context", req.context]
    if req.mgmt_context:
        cmd += ["--mgmt-context", req.mgmt_context]
    if req.cluster_name:
        cmd += ["--cluster-name", req.cluster_name]
    if req.cluster_namespace:
        cmd += ["--cluster-namespace", req.cluster_namespace]
    if req.node_tags:
        cmd += ["--node-tags", req.node_tags]
    if req.ssh_username:
        cmd += ["--ssh-username", req.ssh_username]
    if req.ssh_password:
        cmd += ["--ssh-password", req.ssh_password]
    if req.ssh_key:
        cmd += ["--ssh-key", _expand(req.ssh_key) or req.ssh_key]
    if req.managed_user:
        cmd += ["--managed-user", req.managed_user]
    if req.managed_user_password:
        cmd += ["--managed-user-password", req.managed_user_password]
    if req.domain_suffix:
        cmd += ["--domain-suffix", req.domain_suffix]
    if req.ceph_version:
        cmd += ["--ceph-version", req.ceph_version]
    if req.ceph_image:
        cmd += ["--ceph-image", req.ceph_image]
    if req.dry_run:
        cmd += ["--dry-run"]
    if req.debug:
        cmd += ["--debug"]
    if req.phase:
        cmd += ["--phase", req.phase]

    return cmd


# ---------------------------------------------------------------------------
# Deploy stage activity
# ---------------------------------------------------------------------------

@activity.defn(name="deploy_stage")
def deploy_stage(input: StageInput) -> StageResult:
    """
    Run a single deploy stage by invoking ``daalu deploy --install <stage>``.

    Streams each output line as a heartbeat detail. The parent workflow folds
    those into per-stage log buffers exposed via the `progress` query.
    """
    req = input.request
    stage = input.stage
    cmd = _deploy_command(stage, req)
    return _run_streaming(
        cmd=cmd,
        cwd=req.workspace_root,
        env=_subprocess_env(req),
    )


def _subprocess_env(req: DeployRequest) -> dict:
    env = os.environ.copy()
    # WORKSPACE_ROOT is referenced by daalu's helm chart paths
    env.setdefault("WORKSPACE_ROOT", req.workspace_root)
    # Force unbuffered Python output so log streaming stays real-time
    env["PYTHONUNBUFFERED"] = "1"
    # Disable rich/color since we're streaming line-by-line to a log buffer
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


# ---------------------------------------------------------------------------
# Clean activity
# ---------------------------------------------------------------------------

@activity.defn(name="clean_stage")
def clean_stage(req: CleanRequest) -> StageResult:
    cmd: List[str] = ["daalu", "clean", req.config_path]
    if req.install:
        cmd += ["--install", req.install]
    if req.infra:
        cmd += ["--infra", req.infra]
    if req.kubeconfig:
        cmd += ["--kubeconfig", _expand(req.kubeconfig) or req.kubeconfig]
    if req.mgmt_kubeconfig:
        cmd += ["--mgmt-kubeconfig", _expand(req.mgmt_kubeconfig) or req.mgmt_kubeconfig]
    if req.ssh_key:
        cmd += ["--ssh-key", _expand(req.ssh_key) or req.ssh_key]
    if req.ssh_password:
        cmd += ["--ssh-password", req.ssh_password]
    if req.skip_workload_cluster:
        cmd += ["--skip-workload-cluster"]
    if req.no_wait:
        cmd += ["--no-wait"]
    if req.wipe_mgmt:
        cmd += ["--wipe-mgmt"]
    if req.yes:
        cmd += ["--yes"]
    if req.debug:
        cmd += ["--debug"]

    env = os.environ.copy()
    env.setdefault("WORKSPACE_ROOT", req.workspace_root)
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"

    return _run_streaming(cmd=cmd, cwd=req.workspace_root, env=env)


# ---------------------------------------------------------------------------
# Mirror-images activity
# ---------------------------------------------------------------------------

@activity.defn(name="mirror_images")
def mirror_images(req: MirrorImagesRequest) -> StageResult:
    cmd: List[str] = [
        "daalu", "mirror-images",
        "--harbor-url", req.harbor_url,
        "--config", req.config_path,
    ]
    env = os.environ.copy()
    env.setdefault("WORKSPACE_ROOT", req.workspace_root)
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return _run_streaming(cmd=cmd, cwd=req.workspace_root, env=env)


# Exposed for the worker registration list.
ALL_ACTIVITIES = [deploy_stage, clean_stage, mirror_images]
