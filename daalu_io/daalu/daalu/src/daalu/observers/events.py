# src/daalu/observers/events.py

from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional
from datetime import datetime
import uuid


# ---------------------------------------------------------------------
# Base context and helper
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class BaseEvent:
    ts: str           # ISO timestamp
    run_id: str       # correlates all events in a single deploy invocation
    env: str          # dev/staging/prod/setup/mgmt
    context: Optional[str]  # kube-context

    def dict(self) -> Dict[str, Any]:
        return asdict(self)


def new_ctx(env: str, context: Optional[str]) -> Dict[str, Any]:
    return {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "run_id": str(uuid.uuid4()),
        "env": env,
        "context": context,
    }


# ---------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class PlanComputed(BaseEvent):
    order: List[str]

@dataclass(frozen=True)
class PlanFailed(BaseEvent):
    error: str


# ---------------------------------------------------------------------
# Repository lifecycle
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class RepoAdded(BaseEvent):
    name: str
    url: str

@dataclass(frozen=True)
class ReposUpdated(BaseEvent):
    pass


# ---------------------------------------------------------------------
# Release lifecycle (Helm)
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class ReleaseStarted(BaseEvent):
    name: str
    namespace: str
    chart: str

@dataclass(frozen=True)
class ReleaseLinted(BaseEvent):
    name: str
    ok: bool
    error: Optional[str] = None

@dataclass(frozen=True)
class ReleaseUpgradeAttempt(BaseEvent):
    name: str
    attempt: int

@dataclass(frozen=True)
class ReleaseSucceeded(BaseEvent):
    name: str
    attempts: int
    duration_ms: int

@dataclass(frozen=True)
class ReleaseFailed(BaseEvent):
    name: str
    attempts: int
    error: str


# ---------------------------------------------------------------------
# Waiter lifecycle
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class WaiterStarted(BaseEvent):
    name: str
    namespace: str
    selector: str
    timeout_s: int

@dataclass(frozen=True)
class WaiterSucceeded(BaseEvent):
    name: str

@dataclass(frozen=True)
class WaiterTimedOut(BaseEvent):
    name: str
    timeout_s: int


# ---------------------------------------------------------------------
# Rollback & Summary
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class RollbackStarted(BaseEvent):
    name: str
    namespace: str

@dataclass(frozen=True)
class RollbackResult(BaseEvent):
    name: str
    status: str       # "ROLLED_BACK" | "FAILED"
    error: Optional[str] = None

@dataclass(frozen=True)
class DeploySummary(BaseEvent):
    ok: int
    failed: int
    rolled_back: int


# ---------------------------------------------------------------------
# ClusterAPI lifecycle (management cluster)
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class ClusterAPIStarted(BaseEvent):
    name: str
    namespace: str

@dataclass(frozen=True)
class ManifestApplied(BaseEvent):
    name: str          # manifest filename
    output: str

@dataclass(frozen=True)
class ClusterAPIStatusUpdate(BaseEvent):
    name: str
    output: str

@dataclass(frozen=True)
class ClusterAPIReady(BaseEvent):
    name: str
    namespace: str

@dataclass(frozen=True)
class ClusterAPITimedOut(BaseEvent):
    name: str
    namespace: str
    timeout_s: int

@dataclass(frozen=True)
class ClusterAPIFailed(BaseEvent):
    name: str
    error: str

@dataclass(frozen=True)
class ClusterAPISummary(BaseEvent):
    name: str
    status: str          # "OK" or "FAILED"
    error: Optional[str] = None


# ---------------------------------------------------------------------
# Setup phase lifecycle (setup_manager)
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class SetupStarted(BaseEvent):
    cluster_name: str

@dataclass(frozen=True)
class KubeconfigGenerated(BaseEvent):
    cluster_name: str

@dataclass(frozen=True)
class ControlPlaneDiscovered(BaseEvent):
    cluster_name: str
    ip: str

@dataclass(frozen=True)
class CiliumInstalled(BaseEvent):
    cluster_name: str
    ip: str

@dataclass(frozen=True)
class CiliumReady(BaseEvent):
    cluster_name: str

@dataclass(frozen=True)
class HostsUpdated(BaseEvent):
    cluster_name: str
    count: int

@dataclass(frozen=True)
class NodesLabeled(BaseEvent):
    cluster_name: str
    count: int

@dataclass(frozen=True)
class SetupFailed(BaseEvent):
    cluster_name: str
    error: str

@dataclass(frozen=True)
class SetupSummary(BaseEvent):
    cluster_name: str
    status: str
    error: Optional[str] = None


@dataclass(frozen=True)
class CephStarted(BaseEvent):
    stage: str
    message: str

@dataclass(frozen=True)
class CephProgress(BaseEvent):
    stage: str
    message: str

@dataclass(frozen=True)
class CephFailed(BaseEvent):
    stage: str
    error: str

@dataclass(frozen=True)
class CephSucceeded(BaseEvent):
    stage: str
    message: str


@dataclass
class LifecycleEvent:
    phase: str
    status: str
    message: str