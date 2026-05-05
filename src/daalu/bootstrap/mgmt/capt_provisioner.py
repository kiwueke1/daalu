"""
CAPT (Cluster API Provider Tinkerbell) provisioning logic.

All kubectl / clusterctl / cilium subprocess calls for driving the CAPT
workload-cluster lifecycle live here so that app.py stays thin.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import List, Optional

import typer


# ---------------------------------------------------------------------------
# Phase provisioning
# ---------------------------------------------------------------------------

def provision_phase(
    *,
    phase_name: str,
    namespace: str,
    kc_args: List[str],
    ctx_args: List[str],
    exclude_workflows: Optional[set] = None,
    workflow_name_contains: Optional[str] = None,
    workflow_name_excludes: Optional[str] = None,
    workflow_appear_timeout: int = 120,
    provision_timeout: int = 1800,
    rufio_job_timeout: int = 300,
) -> set:
    """
    For each node that has an active Workflow in this phase:

      1. Rufio Job: off → PXE-once → on   (boot into HookOS, tink-worker runs workflow)
      2. Wait for Workflow → STATE_SUCCESS
      3. Rufio Job: off → on              (reboot into Ubuntu; CAPT sets allowPXE=false
                                           after STATE_SUCCESS so node boots from disk)
      4. Wait for that reboot Job to complete

    Nodes are processed sequentially.

    *workflow_name_contains*: only include workflows whose name contains this substring.
    *workflow_name_excludes*: skip workflows whose name contains this substring.

    Use these together to scope phases cleanly on idempotent re-runs:
      Phase 1 (control-plane): workflow_name_excludes="-workers-"
      Phase 2 (workers):       workflow_name_contains="-workers-"

    Returns the set of workflow names processed in this call.
    """
    _excluded = exclude_workflows or set()

    # ------------------------------------------------------------------
    # Inner helpers
    # ------------------------------------------------------------------

    def _active_workflows() -> list:
        r = subprocess.run(
            [
                "kubectl", *kc_args, *ctx_args,
                "get", "workflow.tinkerbell.org", "-n", namespace,
                "--ignore-not-found",
                "-o",
                "jsonpath={range .items[*]}{.metadata.name}{'|'}{.status.state}{'|'}{.spec.hardwareRef}{'\\n'}{end}",
            ],
            capture_output=True, text=True,
        )
        triples = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) == 3:
                wf_name, state, hw_name = parts[0].strip(), parts[1].strip(), parts[2].strip()
                if not (wf_name and hw_name):
                    continue
                if wf_name in _excluded:
                    continue
                if state not in ("STATE_PENDING", "STATE_RUNNING", "STATE_SUCCESS", ""):
                    continue
                if workflow_name_contains and workflow_name_contains not in wf_name:
                    continue
                if workflow_name_excludes and workflow_name_excludes in wf_name:
                    continue
                triples.append((wf_name, hw_name, state))
        return triples

    def _hardware_uefi(hw_name: str) -> bool:
        r = subprocess.run(
            [
                "kubectl", *kc_args, *ctx_args,
                "get", "hardware.tinkerbell.org", hw_name, "-n", namespace,
                "--ignore-not-found",
                "-o", "jsonpath={.spec.interfaces[0].dhcp.uefi}",
            ],
            capture_output=True, text=True,
        )
        return r.stdout.strip().lower() != "false"

    def _apply_rufio_job(job_name: str, hw_name: str, tasks_yaml: str) -> bool:
        manifest = (
            f"apiVersion: bmc.tinkerbell.org/v1alpha1\n"
            f"kind: Job\n"
            f"metadata:\n"
            f"  name: {job_name}\n"
            f"  namespace: {namespace}\n"
            f"spec:\n"
            f"  machineRef:\n"
            f"    name: {hw_name}\n"
            f"    namespace: {namespace}\n"
            f"  tasks:\n"
            f"{tasks_yaml}"
        )
        r = subprocess.run(
            ["kubectl", *kc_args, *ctx_args, "apply", "-f", "-"],
            input=manifest, text=True, capture_output=True,
        )
        if r.returncode != 0:
            typer.secho(
                f"  [tinkerbell] WARNING: failed to apply Rufio Job {job_name}: {r.stderr.strip()}",
                fg=typer.colors.YELLOW,
            )
        return r.returncode == 0

    def _job_exists(job_name: str) -> bool:
        r = subprocess.run(
            [
                "kubectl", *kc_args, *ctx_args,
                "get", "job.bmc.tinkerbell.org", job_name, "-n", namespace,
                "--ignore-not-found", "-o", "name",
            ],
            capture_output=True, text=True,
        )
        return bool(r.stdout.strip())

    def _wait_rufio_job(job_name: str) -> bool:
        deadline = time.time() + rufio_job_timeout
        while time.time() < deadline:
            r = subprocess.run(
                [
                    "kubectl", *kc_args, *ctx_args,
                    "get", "job.bmc.tinkerbell.org", job_name, "-n", namespace,
                    "--ignore-not-found",
                    "-o", "jsonpath={.status.conditions}",
                ],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                try:
                    for cond in json.loads(r.stdout):
                        if cond.get("type") == "Completed" and cond.get("status") == "True":
                            return True
                        if cond.get("type") == "Failed" and cond.get("status") == "True":
                            typer.secho(
                                f"  [tinkerbell] Rufio Job {job_name} FAILED",
                                fg=typer.colors.YELLOW,
                            )
                            return False
                except Exception:
                    pass
            time.sleep(5)
        typer.secho(
            f"  [tinkerbell] WARNING: Rufio Job {job_name} did not complete within {rufio_job_timeout}s",
            fg=typer.colors.YELLOW,
        )
        return False

    def _wait_workflow(wf_name: str) -> bool:
        deadline = time.time() + provision_timeout
        while time.time() < deadline:
            r = subprocess.run(
                [
                    "kubectl", *kc_args, *ctx_args,
                    "get", "workflow.tinkerbell.org", wf_name, "-n", namespace,
                    "--ignore-not-found",
                    "-o", "jsonpath={.status.state}",
                ],
                capture_output=True, text=True,
            )
            state = r.stdout.strip()
            if state == "STATE_SUCCESS":
                return True
            if state == "STATE_FAILED":
                typer.secho(
                    f"  [tinkerbell] Workflow {wf_name} reached STATE_FAILED",
                    fg=typer.colors.RED,
                )
                return False
            typer.echo(f"  [tinkerbell] [{phase_name}] Workflow {wf_name}: {state or 'PENDING'}")
            time.sleep(20)
        typer.secho(
            f"  [tinkerbell] WARNING: timed out waiting for Workflow {wf_name}",
            fg=typer.colors.YELLOW,
        )
        return False

    # ------------------------------------------------------------------
    # Wait for at least one active workflow to appear
    # ------------------------------------------------------------------
    typer.echo(f"  [tinkerbell] [{phase_name}] Waiting for Workflow(s) to appear...")
    deadline = time.time() + workflow_appear_timeout
    active = []
    while time.time() < deadline:
        active = _active_workflows()
        if active:
            break
        typer.echo(f"  [tinkerbell] [{phase_name}] No Workflows yet — retrying...")
        time.sleep(10)

    if not active:
        typer.secho(
            f"  [tinkerbell] [{phase_name}] WARNING: no Workflows appeared within "
            f"{workflow_appear_timeout}s — skipping phase",
            fg=typer.colors.YELLOW,
        )
        return set()

    typer.echo(
        f"  [tinkerbell] [{phase_name}] Found {len(active)} node(s): "
        f"{[(hw, st) for _, hw, st in active]}"
    )

    processed = set()

    # ------------------------------------------------------------------
    # Process each node sequentially: 4-step sequence
    # ------------------------------------------------------------------
    for wf_name, hw_name, initial_state in active:
        typer.echo(
            f"\n  [tinkerbell] [{phase_name}] === Node: {hw_name} "
            f"(workflow: {wf_name}, state: {initial_state or 'PENDING'}) ==="
        )

        workflow_succeeded = initial_state == "STATE_SUCCESS"

        if not workflow_succeeded:
            pxe_job = f"{hw_name}-deploy-pxe"
            if _job_exists(pxe_job) or initial_state == "STATE_RUNNING":
                typer.echo(
                    f"  [tinkerbell] [{phase_name}] PXE boot already in progress for {hw_name} — skipping"
                )
            else:
                efi = _hardware_uefi(hw_name)
                typer.echo(
                    f"  [tinkerbell] [{phase_name}] Booting {hw_name} into HookOS "
                    f"(PXE, efiBoot={efi})..."
                )
                _apply_rufio_job(
                    pxe_job, hw_name,
                    f"    - powerAction: \"off\"\n"
                    f"    - oneTimeBootDeviceAction:\n"
                    f"        device:\n"
                    f"          - pxe\n"
                    f"        efiBoot: {str(efi).lower()}\n"
                    f"    - powerAction: \"on\"\n",
                )
                _wait_rufio_job(pxe_job)

            typer.echo(
                f"  [tinkerbell] [{phase_name}] Waiting for Workflow {wf_name} → STATE_SUCCESS..."
            )
            if not _wait_workflow(wf_name):
                typer.secho(
                    f"  [tinkerbell] [{phase_name}] Skipping disk reboot for {hw_name} "
                    f"— workflow did not succeed",
                    fg=typer.colors.YELLOW,
                )
                continue
            workflow_succeeded = True

        typer.secho(
            f"  [tinkerbell] [{phase_name}] Workflow {wf_name} SUCCESS",
            fg=typer.colors.GREEN,
        )

        disk_job = f"{hw_name}-reboot-to-disk"
        if _job_exists(disk_job):
            typer.echo(
                f"  [tinkerbell] [{phase_name}] Disk reboot Job {disk_job} already exists — skipping"
            )
        else:
            typer.echo(f"  [tinkerbell] [{phase_name}] Rebooting {hw_name} into Ubuntu (disk)...")
            _apply_rufio_job(
                disk_job, hw_name,
                f"    - powerAction: \"off\"\n"
                f"    - powerAction: \"on\"\n",
            )

        _wait_rufio_job(disk_job)
        typer.secho(
            f"  [tinkerbell] [{phase_name}] {hw_name} rebooted into Ubuntu.",
            fg=typer.colors.GREEN,
        )
        processed.add(wf_name)

    return processed


# ---------------------------------------------------------------------------
# KCP initialization check
# ---------------------------------------------------------------------------

def wait_for_kcp_initialized(
    *,
    cluster_name: str,
    namespace: str,
    kc_args: List[str],
    ctx_args: List[str],
    timeout: int = 1800,
) -> bool:
    """
    Wait until the CAPI kubeconfig secret exists (created by CAPI immediately
    after kubeadm init succeeds on the first control-plane node).

    Using the secret as the signal is more reliable than status.initialized
    (returns empty via jsonpath with CAPI v1beta2 API serving) or
    readyReplicas (stays 0 until CNI is installed, which happens after this step).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            [
                "kubectl", *kc_args, *ctx_args,
                "get", "secret", f"{cluster_name}-kubeconfig",
                "-n", namespace,
                "--ignore-not-found",
                "-o", "name",
            ],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            typer.secho(
                f"  [tinkerbell] KubeadmControlPlane initialized "
                f"(kubeconfig secret {cluster_name}-kubeconfig exists).",
                fg=typer.colors.GREEN,
            )
            return True
        typer.echo(
            f"  [tinkerbell] Waiting for kubeconfig secret {cluster_name}-kubeconfig "
            f"(kubeadm init not yet complete)..."
        )
        time.sleep(20)

    typer.secho(
        f"  [tinkerbell] WARNING: KCP did not initialize within {timeout}s",
        fg=typer.colors.YELLOW,
    )
    return False


def wait_for_kcp_ready(
    *,
    cluster_name: str,
    namespace: str,
    kc_args: List[str],
    ctx_args: List[str],
    timeout: int = 600,
) -> bool:
    """
    Wait until KubeadmControlPlane has at least one Ready replica.

    CAPT only starts scheduling worker Machines after KCP reports readyReplicas >= 1,
    which requires the control-plane node to be Ready (i.e. CNI installed and running).
    This must be called AFTER Cilium is installed and before the worker phase.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            [
                "kubectl", *kc_args, *ctx_args,
                "get", "kubeadmcontrolplane", cluster_name,
                "-n", namespace,
                "--ignore-not-found",
                "-o", "jsonpath={.status.readyReplicas}",
            ],
            capture_output=True, text=True,
        )
        val = r.stdout.strip()
        if val and int(val) >= 1:
            typer.secho(
                f"  [tinkerbell] KubeadmControlPlane {cluster_name} has {val} ready replica(s) — "
                "CAPT will now schedule workers.",
                fg=typer.colors.GREEN,
            )
            return True
        typer.echo(
            f"  [tinkerbell] Waiting for KCP readyReplicas >= 1 (current: {val or '0'})..."
        )
        time.sleep(15)

    typer.secho(
        f"  [tinkerbell] WARNING: KCP {cluster_name} did not reach readyReplicas=1 within {timeout}s",
        fg=typer.colors.YELLOW,
    )
    return False


# ---------------------------------------------------------------------------
# Kubeconfig fetch
# ---------------------------------------------------------------------------

def fetch_workload_kubeconfig(
    *,
    cluster_name: str,
    namespace: str,
    kc_args: List[str],
    ctx_args: List[str],
    output_path: Optional[str] = None,
) -> Optional[str]:
    """
    Fetch the workload cluster kubeconfig via clusterctl and write it to
    *output_path* (defaults to /tmp/kubeconfig-<cluster_name>.yaml).

    Returns the path on success, None on failure.
    """
    out = output_path or f"/tmp/kubeconfig-{cluster_name}.yaml"
    r = subprocess.run(
        ["clusterctl", *kc_args, *ctx_args,
         "get", "kubeconfig", cluster_name, "-n", namespace],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        Path(out).write_text(r.stdout)
        typer.secho(
            f"  [tinkerbell] Workload kubeconfig written to {out}",
            fg=typer.colors.GREEN,
        )
        return out
    typer.secho(
        f"  [tinkerbell] WARNING: could not fetch kubeconfig: {r.stderr.strip()}",
        fg=typer.colors.YELLOW,
    )
    return None


# ---------------------------------------------------------------------------
# CNI install
# ---------------------------------------------------------------------------

def install_cilium(*, version: str, kubeconfig: str) -> bool:
    """
    Run `cilium install --version <version>` then `cilium status --wait`
    so that we block until Cilium pods are running and nodes can become Ready.
    Returns True on success.
    """
    typer.echo(f"\n  [tinkerbell] Installing Cilium {version}...")
    r = subprocess.run(
        ["cilium", "install", "--version", version, "--kubeconfig", kubeconfig],
        text=True, capture_output=True,
    )
    if r.returncode != 0:
        # "cannot re-use a name that is still in use" means Cilium is already installed
        if "re-use a name" in r.stderr or "re-use a name" in r.stdout:
            typer.echo("  [tinkerbell] Cilium already installed — upgrading...")
            r = subprocess.run(
                ["cilium", "upgrade", "--version", version, "--kubeconfig", kubeconfig],
                text=True,
            )
        if r.returncode != 0:
            typer.secho(
                "  [tinkerbell] WARNING: cilium install/upgrade exited non-zero — "
                "nodes may stay NotReady",
                fg=typer.colors.YELLOW,
            )
            return False

    typer.echo("  [tinkerbell] Waiting for Cilium to become healthy...")
    r2 = subprocess.run(
        ["cilium", "status", "--wait", "--kubeconfig", kubeconfig],
        text=True,
    )
    if r2.returncode != 0:
        typer.secho(
            "  [tinkerbell] WARNING: cilium status --wait exited non-zero — "
            "nodes may stay NotReady",
            fg=typer.colors.YELLOW,
        )
        return False

    typer.secho("  [tinkerbell] Cilium healthy.", fg=typer.colors.GREEN)
    return True


# ---------------------------------------------------------------------------
# Workload API server stability gate
# ---------------------------------------------------------------------------

def wait_for_workload_api_server_stable(
    *,
    workload_kubeconfig: str,
    consecutive_ok: int = 3,
    poll_interval: int = 10,
    timeout: int = 600,
) -> bool:
    """
    Poll the workload cluster API server until it responds successfully to
    ``kubectl get nodes`` *consecutive_ok* times in a row.

    kubeadm init creates the CAPI kubeconfig secret early — while the API
    server is still initialising and may restart as kubeadm progresses
    through its phases.  The node can also undergo a second OS-level reboot
    (e.g. cloud-init/systemd finalising network config) shortly after the
    workflow completes.  Installing Cilium during that window fails because
    it can't reach the API server.

    Waiting for *consecutive_ok* consecutive healthy responses (rather than
    a single success) guards against transient blips where the API server
    responds once then goes down again during a restart.
    """
    deadline = time.time() + timeout
    streak = 0
    while time.time() < deadline:
        r = subprocess.run(
            ["kubectl", "--kubeconfig", workload_kubeconfig, "get", "nodes"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            streak += 1
            typer.echo(
                f"  [tinkerbell] Workload API server healthy "
                f"({streak}/{consecutive_ok} consecutive ok)..."
            )
            if streak >= consecutive_ok:
                typer.secho(
                    "  [tinkerbell] Workload API server stable — proceeding with Cilium install.",
                    fg=typer.colors.GREEN,
                )
                return True
        else:
            if streak > 0:
                typer.echo(
                    "  [tinkerbell] Workload API server became unreachable — "
                    "node may be rebooting; resetting streak and waiting..."
                )
            streak = 0
        time.sleep(poll_interval)

    typer.secho(
        f"  [tinkerbell] WARNING: workload API server did not stabilise within {timeout}s",
        fg=typer.colors.YELLOW,
    )
    return False


# ---------------------------------------------------------------------------
# Final cluster-ready wait
# ---------------------------------------------------------------------------

def wait_for_cluster_ready(
    *,
    workload_kubeconfig: str,
    expected_nodes: int,
    timeout: int = 1800,
) -> bool:
    """
    Wait until *expected_nodes* nodes in the workload cluster all have Ready=True.

    Queries the workload cluster directly (via workload_kubeconfig) rather than
    checking CAPI readyReplicas on the management cluster — CAPI won't mark machines
    Ready until providerID is reconciled, but the nodes are usable as soon as they
    report Ready to the API server.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            [
                "kubectl", "--kubeconfig", workload_kubeconfig,
                "get", "nodes",
                "-o", "jsonpath={range .items[*]}"
                      "{.metadata.name}{'='}"
                      "{.status.conditions[?(@.type==\"Ready\")].status}{'\\n'}"
                      "{end}",
            ],
            capture_output=True, text=True,
        )
        lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
        ready_count = sum(1 for l in lines if l.endswith("=True"))
        total_count = len(lines)
        typer.echo(
            f"  [tinkerbell] cluster nodes ready: {ready_count}/{total_count} "
            f"(expecting {expected_nodes})"
        )
        if ready_count >= expected_nodes:
            typer.secho("  [tinkerbell] Cluster fully ready.", fg=typer.colors.GREEN)
            return True
        time.sleep(20)

    typer.secho(
        f"  [tinkerbell] WARNING: cluster did not become ready within {timeout}s",
        fg=typer.colors.YELLOW,
    )
    return False
