from daalu.config.models import ClusterConfig, ReleaseSpec
from daalu.deploy.executor import deploy_all, DeployOptions
from daalu.observers.dispatcher import EventBus
from daalu.observers.interface import Observer
from daalu.observers.events import ReleaseStarted, ReleaseSucceeded, DeploySummary

# Simple capturing observer
class Capture(Observer):
    def __init__(self): self.events = []
    def notify(self, event): self.events.append(event)

class FakeHelm:
    def add_repo(self, repo): pass
    def update_repos(self): pass
    def lint(self, rel): pass
    def upgrade_install(self, rel): pass
    def uninstall(self, name, ns): pass
    def diff(self, rel): return ""

def test_observer_receives_events():
    cfg = ClusterConfig(environment="dev", repos=[], releases=[
        ReleaseSpec(name="a", namespace="ns", chart="repo/a")
    ])

    cap = Capture()
    report = deploy_all(cfg, FakeHelm(), waiter=None, options=DeployOptions(), observers=[cap])

    kinds = {e.__class__.__name__ for e in cap.events}
    assert "PlanComputed" in kinds
    assert "ReleaseStarted" in kinds
    assert "ReleaseSucceeded" in kinds
    assert "DeploySummary" in kinds
