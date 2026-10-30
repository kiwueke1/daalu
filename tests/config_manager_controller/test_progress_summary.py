"""Which container is actually blocking a pod.

The distinction that matters operationally: a pod sitting in ``Init:*`` is
making progress, while ``ImagePullBackOff`` or an unschedulable Pending pod is
stuck. The UI colours them differently, so the reason string has to name the
*init* container when one is still running — reporting the main container's
generic "PodInitializing" instead would hide the real cause.
"""

from __future__ import annotations

from types import SimpleNamespace

from daalu_automation.config_manager_controller.progress import _container_summary


def _state(*, waiting=None, terminated=None, running=None):
    return SimpleNamespace(waiting=waiting, terminated=terminated, running=running)


def _cs(ready=False, **state_kw):
    return SimpleNamespace(ready=ready, state=_state(**state_kw))


def _pod(containers, inits=None):
    return SimpleNamespace(
        status=SimpleNamespace(
            container_statuses=containers, init_container_statuses=inits or []
        )
    )


def test_all_ready_has_no_blocking_reason():
    pod = _pod([_cs(ready=True), _cs(ready=True)])
    assert _container_summary(pod) == (2, 2, None)


def test_unfinished_init_container_wins_over_main_container():
    # The main container always reports PodInitializing while an init runs;
    # the init's own reason is the informative one.
    pod = _pod(
        [_cs(ready=False, waiting=SimpleNamespace(reason="PodInitializing"))],
        inits=[_cs(waiting=SimpleNamespace(reason="ImagePullBackOff"))],
    )
    assert _container_summary(pod) == (0, 1, "Init:ImagePullBackOff")


def test_running_init_container_reports_init_running():
    pod = _pod(
        [_cs(ready=False, waiting=SimpleNamespace(reason="PodInitializing"))],
        inits=[_cs(running=SimpleNamespace(started_at="x"))],
    )
    assert _container_summary(pod)[2] == "Init:Running"


def test_failed_init_container_is_surfaced():
    pod = _pod(
        [_cs(ready=False)],
        inits=[_cs(terminated=SimpleNamespace(exit_code=1, reason="Error"))],
    )
    assert _container_summary(pod)[2] == "Init:Error"


def test_succeeded_init_container_does_not_block():
    # exit_code 0 means the init finished; the main container's state governs.
    pod = _pod(
        [_cs(ready=False, waiting=SimpleNamespace(reason="ImagePullBackOff"))],
        inits=[_cs(terminated=SimpleNamespace(exit_code=0, reason="Completed"))],
    )
    assert _container_summary(pod)[2] == "ImagePullBackOff"


def test_main_container_waiting_reason_when_no_inits():
    pod = _pod([_cs(ready=False, waiting=SimpleNamespace(reason="ContainerCreating"))])
    assert _container_summary(pod) == (0, 1, "ContainerCreating")


def test_pod_with_no_container_statuses_is_not_an_error():
    # A freshly-scheduled (or unschedulable) pod has no statuses yet.
    pod = _pod([], inits=[])
    assert _container_summary(pod) == (0, 0, None)


def test_partial_readiness_is_counted():
    pod = _pod([_cs(ready=True), _cs(ready=False, waiting=SimpleNamespace(reason="CrashLoopBackOff"))])
    ready, total, reason = _container_summary(pod)
    assert (ready, total) == (1, 2)
    assert reason == "CrashLoopBackOff"
