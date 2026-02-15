from dataclasses import dataclass
from typing import Optional, Dict, List

from daalu.deploy.executor import deploy_all, DeployOptions
from daalu.config.models import ClusterConfig, RepoSpec, ReleaseSpec
from daalu.observers.dispatcher import EventBus
from daalu.observers.events import (
    PlanComputed, RepoAdded, ReposUpdated,
    ReleaseStarted, ReleaseLinted, ReleaseUpgradeAttempt,
    ReleaseSucceeded, ReleaseFailed, RollbackStarted, RollbackResult, DeploySummary,
    WaiterStarted, WaiterSucceeded, WaiterTimedOut,
)

# --------- Test doubles ----------

@dataclass
class Call:
    op: str
    args: tuple

class FakeHelm:
    """A controllable fake Helm client that can fail specific releases a number of times."""
    def __init__(self, fail_on_release: Optional[str] = None, fail_times: int = 0):
        self.calls: List[Call] = []
        self.fail_on_release = fail_on_release
        self.fail_times = fail_times
        self._attempts: Dict[str, int] = {}

    def add_repo(self, repo):
        self.calls.append(Call("add_repo", (repo.name, str(repo.url))))

    def update_repos(self):
        self.calls.append(Call("update_repos", ()))

    def lint(self, rel):
        self.calls.append(Call("lint", (rel.name,)))

    def upgrade_install(self, rel):
        self.calls.append(Call("upgrade", (rel.name,)))
        if rel.name == self.fail_on_release:
            count = self._attempts.get(rel.name, 0) + 1
            self._attempts[rel.name] = count
            if count <= self.fail_times:
                raise RuntimeError(f"boom {rel.name} attempt {count}")

    def uninstall(self, name, ns):
        self.calls.append(Call("uninstall", (name, ns)))

    def diff(self, rel):
        return ""


class Capture:
    def __init__(self): self.events = []
    def notify(self, ev): self.events.append(ev)


# --------- Tests ----------

def test_happy_path_emits_events_and_succeeds():
    cfg = ClusterConfig(
        environment="dev",
        repos=[RepoSpec(name="r", url="https://example.test")],
        releases=[ReleaseSpec(name="a", namespace="ns", chart="repo/a")]
    )

    helm = FakeHelm()
    cap = Capture()

    report = deploy_all(cfg, helm, observers=[cap], options=DeployOptions())
    assert report.summary().startswith("OK=1")

    kinds = {e.__class__.__name__ for e in cap.events}
    for k in ["PlanComputed", "RepoAdded", "ReposUpdated", "ReleaseStarted",
              "ReleaseLinted", "ReleaseUpgradeAttempt", "ReleaseSucceeded", "DeploySummary"]:
        assert k in kinds, f"missing event {k}"


def test_retry_then_success_records_attempts_and_events():
    cfg = ClusterConfig(environment="dev", repos=[], releases=[
        ReleaseSpec(name="x", namespace="ns", chart="repo/x")
    ])

    helm = FakeHelm(fail_on_release="x", fail_times=2)  # fail twice, succeed on 3rd
    cap = Capture()

    report = deploy_all(cfg, helm, observers=[cap], options=DeployOptions(retries=3, backoff_seconds=0.01))
    assert "OK=1" in report.summary()

    # Count attempts events
    attempts = sum(1 for e in cap.events if isinstance(e, ReleaseUpgradeAttempt))
    assert attempts == 3

    # Ensure we saw a success event
    assert any(isinstance(e, ReleaseSucceeded) and e.name == "x" for e in cap.events)


def test_failure_triggers_rollback_and_events():
    cfg = ClusterConfig(environment="dev", repos=[], releases=[
        ReleaseSpec(name="a", namespace="ns", chart="repo/a"),
        ReleaseSpec(name="b", namespace="ns", chart="repo/b", dependencies=["a"]),
    ])

    helm = FakeHelm(fail_on_release="b", fail_times=10)  # always fail b
    cap = Capture()

    report = deploy_all(cfg, helm, observers=[cap], options=DeployOptions(retries=1, backoff_seconds=0.01))
    # 'a' should be rolled back
    assert any(c.op == "uninstall" and c.args == ("a", "ns") for c in helm.calls)

    kinds = [e.__class__.__name__ for e in cap.events]
    assert "ReleaseFailed" in kinds
    assert "RollbackStarted" in kinds
    assert "RollbackResult" in kinds
    assert "DeploySummary" in kinds


def test_waiter_success_and_timeout_events():
    # Release 'w' will pass; 't' will timeout in waiter
    cfg = ClusterConfig(environment="dev", repos=[], releases=[
        ReleaseSpec(name="w", namespace="ns", chart="repo/w"),
        ReleaseSpec(name="t", namespace="ns", chart="repo/t", dependencies=["w"]),
    ])
    helm = FakeHelm()
    cap = Capture()

    def waiter_ok(ns, selector, timeout_s, ctx):
        return  # success

    def waiter_timeout(ns, selector, timeout_s, ctx):
        raise TimeoutError("nope")

    # First run: waiter OK
    report1 = deploy_all(
        cfg=ClusterConfig(environment="dev", repos=[], releases=[cfg.releases[0]]),
        helm=helm,
        observers=[cap],
        options=DeployOptions(use_waiter=True),
        waiter=waiter_ok,
    )
    assert any(e.__class__.__name__ == "WaiterSucceeded" for e in cap.events)

    # Second run: waiter times out on 't'
    cap2 = Capture()
    try:
        deploy_all(
            cfg=ClusterConfig(environment="dev", repos=[], releases=cfg.releases),
            helm=helm,
            observers=[cap2],
            options=DeployOptions(use_waiter=True, retries=0, backoff_seconds=0.01),
            waiter=waiter_timeout,
        )
        assert False, "Expected TimeoutError to bubble"
    except TimeoutError:
        assert any(e.__class__.__name__ == "WaiterTimedOut" for e in cap2.events)
