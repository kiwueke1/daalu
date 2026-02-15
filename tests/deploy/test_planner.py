from daalu.deploy.planner import plan, UnknownDependencyError, CyclicDependencyError
from daalu.config.models import ClusterConfig, ReleaseSpec
from daalu.observers.dispatcher import EventBus
from daalu.observers.events import PlanComputed, PlanFailed

class Capture:
    def __init__(self): self.events = []
    def notify(self, ev): self.events.append(ev)

def _cfg(releases):
    return ClusterConfig(environment="dev", repos=[], releases=releases)

def test_plan_orders_dependencies_and_emits_event():
    a = ReleaseSpec(name="a", namespace="ns", chart="repo/a")
    b = ReleaseSpec(name="b", namespace="ns", chart="repo/b", dependencies=["a"])
    c = ReleaseSpec(name="c", namespace="ns", chart="repo/c", dependencies=["b"])
    cap = Capture()
    ordered = plan(_cfg([c, b, a]), bus=EventBus([cap]))
    assert [r.name for r in ordered] == ["a", "b", "c"]
    kinds = {e.__class__.__name__ for e in cap.events}
    assert "PlanComputed" in kinds
    pc = next(e for e in cap.events if isinstance(e, PlanComputed))
    assert pc.order == ["a", "b", "c"]

def test_plan_unknown_dep_raises_and_emits_failure():
    bad = ReleaseSpec(name="x", namespace="ns", chart="repo/x", dependencies=["missing"])
    cap = Capture()
    try:
        plan(_cfg([bad]), bus=EventBus([cap]))
        assert False, "expected UnknownDependencyError"
    except UnknownDependencyError:
        kinds = {e.__class__.__name__ for e in cap.events}
        assert "PlanFailed" in kinds
        pf = next(e for e in cap.events if isinstance(e, PlanFailed))
        assert "unknown release" in pf.error

def test_plan_cycle_detected_and_emits_failure():
    a = ReleaseSpec(name="a", namespace="ns", chart="repo/a", dependencies=["b"])
    b = ReleaseSpec(name="b", namespace="ns", chart="repo/b", dependencies=["a"])
    cap = Capture()
    try:
        plan(_cfg([a, b]), bus=EventBus([cap]))
        assert False, "expected CyclicDependencyError"
    except CyclicDependencyError:
        kinds = {e.__class__.__name__ for e in cap.events}
        assert "PlanFailed" in kinds
