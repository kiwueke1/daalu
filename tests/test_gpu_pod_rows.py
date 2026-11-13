"""The Add GPU pod view reads like ``kubectl get pods`` and colours by health.

Pins the STATUS column rules (kubectl's) and the health word the UI colours
red/green by, on fake pods shaped like the kubernetes client's objects.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Ns

from daalu_automation.gpu_controller.k8s import pod_row

NOW = datetime(2026, 11, 13, 7, 0, tzinfo=timezone.utc)


def _state(running=False, waiting=None, terminated=None):
    return Ns(
        running=Ns() if running else None,
        waiting=Ns(reason=waiting, message=None) if waiting else None,
        terminated=Ns(reason=terminated[0], exit_code=terminated[1], message=None)
        if terminated
        else None,
    )


def _cs(name, *, ready=False, restarts=0, **state):
    return Ns(name=name, ready=ready, restart_count=restarts, state=_state(**state))


def _pod(name, phase, *, inits=(), mains=(), init_names=(), main_names=("main",), deleting=False):
    return Ns(
        metadata=Ns(
            name=name,
            creation_timestamp=NOW - timedelta(seconds=90),
            deletion_timestamp=NOW if deleting else None,
        ),
        spec=Ns(
            node_name="gpu-node-1",
            init_containers=[Ns(name=n) for n in init_names],
            containers=[Ns(name=n) for n in main_names],
        ),
        status=Ns(phase=phase, init_container_statuses=list(inits), container_statuses=list(mains)),
    )


def test_running_and_ready_is_green():
    r = pod_row(_pod("dcgm", "Running", mains=[_cs("main", ready=True, running=True)]), now=NOW)
    assert (r["ready"], r["status"], r["health"]) == ("1/1", "Running", "ready")
    assert r["age_seconds"] == 90
    assert r["log_container"] == "main"


def test_init_progress_reads_like_kubectl():
    p = _pod(
        "validator",
        "Pending",
        init_names=("driver-validation", "toolkit-validation", "cuda-validation", "plugin-validation"),
        inits=[
            _cs("driver-validation", terminated=("Completed", 0)),
            _cs("toolkit-validation", running=True),
            _cs("cuda-validation", waiting="PodInitializing"),
            _cs("plugin-validation", waiting="PodInitializing"),
        ],
        mains=[_cs("main", waiting="PodInitializing")],
    )
    r = pod_row(p, now=NOW)
    assert r["status"] == "Init:1/4"
    assert r["health"] == "starting"
    assert r["log_container"] == "toolkit-validation"  # the one doing work
    assert r["containers"][:2] == ["driver-validation", "toolkit-validation"]


def test_stuck_init_container_is_red():
    p = _pod(
        "plugin",
        "Pending",
        init_names=("toolkit-validation",),
        inits=[_cs("toolkit-validation", waiting="CrashLoopBackOff", restarts=4)],
    )
    r = pod_row(p, now=NOW)
    assert r["status"] == "Init:CrashLoopBackOff"
    assert r["health"] == "failing"
    assert r["restarts"] == 4


def test_container_creating_is_starting_not_failing():
    r = pod_row(_pod("vllm", "Pending", mains=[_cs("main", waiting="ContainerCreating")]), now=NOW)
    assert (r["status"], r["health"]) == ("ContainerCreating", "starting")


def test_image_pull_backoff_is_failing():
    r = pod_row(_pod("vllm", "Pending", mains=[_cs("main", waiting="ImagePullBackOff")]), now=NOW)
    assert (r["status"], r["health"]) == ("ImagePullBackOff", "failing")


def test_finished_job_pod_is_completed():
    p = _pod("cuda-validator", "Succeeded", mains=[_cs("main", terminated=("Completed", 0))])
    r = pod_row(p, now=NOW)
    assert (r["ready"], r["status"], r["health"]) == ("0/1", "Completed", "done")


def test_deleting_pod_says_terminating():
    p = _pod("old", "Running", mains=[_cs("main", ready=True, running=True)], deleting=True)
    assert pod_row(p, now=NOW)["status"] == "Terminating"
