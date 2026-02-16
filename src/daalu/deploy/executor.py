# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .planner import plan
from .hooks import has as has_hook, get as get_hook
from ..helm.interface import IHelm
from ..config.models import ClusterConfig, ReleaseSpec

# Observer bits
from ..observers.dispatcher import EventBus
from ..observers.events import (
    new_ctx,
    RepoAdded,
    ReposUpdated,
    ReleaseStarted,
    ReleaseLinted,
    ReleaseUpgradeAttempt,
    ReleaseSucceeded,
    ReleaseFailed,
    WaiterStarted,
    WaiterSucceeded,
    WaiterTimedOut,
    RollbackStarted,
    RollbackResult,
    DeploySummary,
)

log = logging.getLogger("daalu")


@dataclass
class DeployOptions:
    retries: int = 2
    backoff_seconds: float = 2.0
    use_waiter: bool = False
    waiter_selector_key: str = "app"
    debug: bool = True


@dataclass
class ReleaseOutcome:
    name: str
    namespace: str
    status: str                 # "OK" | "FAILED" | "ROLLED_BACK"
    attempts: int = 0
    error: Optional[str] = None


@dataclass
class DeployReport:
    outcomes: List[ReleaseOutcome] = field(default_factory=list)

    def add(self, outcome: ReleaseOutcome) -> None:
        self.outcomes.append(outcome)

    def summary(self) -> str:
        ok = sum(1 for o in self.outcomes if o.status == "OK")
        failed = sum(1 for o in self.outcomes if o.status == "FAILED")
        rolled = sum(1 for o in self.outcomes if o.status == "ROLLED_BACK")
        return f"OK={ok} FAILED={failed} ROLLED_BACK={rolled}"


def _maybe_call_hooks(rel: ReleaseSpec, phase: str, context: Dict) -> None:
    for hook_name in rel.hooks:
        if not has_hook(hook_name):
            continue
        hook_fn = get_hook(hook_name)
        hook_fn(rel, phase, context)  # type: ignore[call-arg]


def _retry(times: int, backoff: float, fn: Callable[[], None]) -> int:
    attempt = 0
    while True:
        attempt += 1
        try:
            fn()
            return attempt
        except Exception:
            if attempt > times:
                raise
            time.sleep(backoff)


def deploy_all(
    cfg: ClusterConfig,
    helm: IHelm,
    waiter: Optional[Callable[[str, str, int, Optional[str]], None]] = None,
    options: Optional[DeployOptions] = None,
    observers: Optional[List] = None,
) -> DeployReport:
    """
    Deploy in topological order. Emits observer events if observers are provided.
    """
    options = options or DeployOptions()
    report = DeployReport()
    deployed_stack: List[ReleaseSpec] = []

    # Observer bus & run context
    bus = EventBus(observers or [])
    run_ctx = new_ctx(env=cfg.environment, context=cfg.context)

    # 1) Repos
    log.debug("starting repo update")
    for repo in cfg.repos:
        helm.add_repo(repo, debug=options.debug)
        bus.emit(RepoAdded(name=repo.name, url=str(repo.url), **run_ctx))
    helm.update_repos(debug=options.debug)
    bus.emit(ReposUpdated(**run_ctx))

    # 2) Plan (planner also emits PlanComputed/PlanFailed)
    log.debug("making plan")
    ordered = plan(cfg, bus=bus, run_ctx=run_ctx)
    log.debug(ordered)

    try:
        log.debug("deploying")
        # 3) Deploy in order
        for rel in ordered:
            bus.emit(ReleaseStarted(name=rel.name, namespace=rel.namespace, chart=rel.chart, **run_ctx))

            _maybe_call_hooks(rel, "pre", {"context": cfg.context})

            # Lint
            try:
                helm.lint(rel, debug=options.debug)
                bus.emit(ReleaseLinted(name=rel.name, ok=True, error=None, **run_ctx))
            except Exception as lint_err:
                bus.emit(ReleaseLinted(name=rel.name, ok=False, error=str(lint_err), **run_ctx))
                raise

            # Upgrade/install with retry
            outcome = ReleaseOutcome(name=rel.name, namespace=rel.namespace, status="FAILED")
            def _do_upgrade():
                helm.upgrade_install(rel, debug=options.debug)

            attempts = 0
            while True:
                attempts += 1
                bus.emit(ReleaseUpgradeAttempt(name=rel.name, attempt=attempts, **run_ctx))
                try:
                    t0 = time.time()
                    _do_upgrade()
                    duration_ms = int((time.time() - t0) * 1000)
                    outcome.attempts = attempts
                    outcome.status = "OK"
                    report.add(outcome)
                    bus.emit(ReleaseSucceeded(name=rel.name, attempts=attempts, duration_ms=duration_ms, **run_ctx))
                    break
                except Exception as e:
                    if attempts <= options.retries:
                        time.sleep(options.backoff_seconds)
                        continue
                    outcome.attempts = attempts
                    outcome.error = str(e)
                    report.add(outcome)
                    bus.emit(ReleaseFailed(name=rel.name, attempts=attempts, error=str(e), **run_ctx))
                    raise

            deployed_stack.append(rel)

            # Optional extra waiter
            if options.use_waiter and waiter is not None:
                selector = f"{options.waiter_selector_key}={rel.name}"
                timeout = rel.timeout_seconds if rel.timeout_seconds else 300
                bus.emit(WaiterStarted(name=rel.name, namespace=rel.namespace, selector=selector, timeout_s=timeout, **run_ctx))
                try:
                    waiter(rel.namespace, selector, timeout, cfg.context)
                    bus.emit(WaiterSucceeded(name=rel.name, **run_ctx))
                except TimeoutError:
                    bus.emit(WaiterTimedOut(name=rel.name, timeout_s=timeout, **run_ctx))
                    raise

            _maybe_call_hooks(rel, "post", {"context": cfg.context})

        # Final summary on success
        ok = sum(1 for o in report.outcomes if o.status == "OK")
        failed = sum(1 for o in report.outcomes if o.status == "FAILED")
        rolled = sum(1 for o in report.outcomes if o.status == "ROLLED_BACK")
        bus.emit(DeploySummary(ok=ok, failed=failed, rolled_back=rolled, **run_ctx))
        return report

    except Exception as e:
        # 4) Best-effort rollback
        err_msg = str(e)
        if report.outcomes and report.outcomes[-1].status != "OK":
            report.outcomes[-1].error = err_msg

        for rel in reversed(deployed_stack):
            bus.emit(RollbackStarted(name=rel.name, namespace=rel.namespace, **run_ctx))
            try:
                helm.uninstall(rel.name, rel.namespace, debug=options.debug)
                report.add(ReleaseOutcome(name=rel.name, namespace=rel.namespace, status="ROLLED_BACK"))
                bus.emit(RollbackResult(name=rel.name, status="ROLLED_BACK", error=None, **run_ctx))
            except Exception as re:
                report.add(ReleaseOutcome(name=rel.name, namespace=rel.namespace, status="FAILED", error=str(re)))
                bus.emit(RollbackResult(name=rel.name, status="FAILED", error=str(re), **run_ctx))

        # Summary after rollback attempt
        ok = sum(1 for o in report.outcomes if o.status == "OK")
        failed = sum(1 for o in report.outcomes if o.status == "FAILED")
        rolled = sum(1 for o in report.outcomes if o.status == "ROLLED_BACK")
        bus.emit(DeploySummary(ok=ok, failed=failed, rolled_back=rolled, **run_ctx))

        if not report.outcomes or report.outcomes[-1].status == "OK":
            report.add(ReleaseOutcome(name="__executor__", namespace="-", status="FAILED", error=err_msg))
        return report
