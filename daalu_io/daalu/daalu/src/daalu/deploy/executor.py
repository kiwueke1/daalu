# src/daalu/deploy/executor.py
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .planner import plan
from .hooks import has as has_hook, get as get_hook
from ..helm.interface import IHelm
from ..config.models import ClusterConfig, ReleaseSpec


@dataclass
class DeployOptions:
    retries: int = 2
    backoff_seconds: float = 2.0
    use_waiter: bool = False              # call wait_for_rollout after helm --wait
    waiter_selector_key: str = "app"      # label key to use for default selector


@dataclass
class ReleaseOutcome:
    name: str
    namespace: str
    status: str                 # "OK" or "FAILED" or "ROLLED_BACK"
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
    """
    Run any hooks listed on the release.
    You can encode naming conventions like '<name>:pre' or '<name>:post' inside rel.hooks.
    """
    for hook_name in rel.hooks:
        # Convention: allow "name@pre" / "name@post" filtering here if you want.
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


def deploy_all(cfg: ClusterConfig, helm: IHelm, waiter: Optional[Callable[[str, str, int, Optional[str]], None]] = None, options: Optional[DeployOptions] = None) -> DeployReport:
    """
    Deploy all releases in topological order using the provided Helm implementation.

    - Adds/updates repos
    - For each release:
        * pre-hooks
        * helm lint
        * helm upgrade --install (with retries)
        * optional waiter for additional readiness
        * post-hooks
    - If anything fails, uninstall the releases deployed in this session (reverse order).

    Returns:
        DeployReport with per-release outcomes.
    """
    options = options or DeployOptions()
    report = DeployReport()
    deployed_stack: List[ReleaseSpec] = []

    # 1) Repos
    for repo in cfg.repos:
        helm.add_repo(repo)
    helm.update_repos()

    # 2) Plan
    ordered = plan(cfg)

    try:
        # 3) Deploy in order
        for rel in ordered:
            _maybe_call_hooks(rel, "pre", {"context": cfg.context})

            # Lint (fail fast)
            helm.lint(rel)

            # Upgrade/install with retry
            outcome = ReleaseOutcome(name=rel.name, namespace=rel.namespace, status="FAILED")
            def _do_upgrade():
                helm.upgrade_install(rel)
            attempts = _retry(options.retries, options.backoff_seconds, _do_upgrade)
            outcome.attempts = attempts

            # Optional extra waiter
            if options.use_waiter and waiter is not None:
                # Basic default selector: "app=<release name>"
                selector = f"{options.waiter_selector_key}={rel.name}"
                timeout = rel.timeout_seconds if rel.timeout_seconds else 300
                waiter(rel.namespace, selector, timeout, cfg.context)

            outcome.status = "OK"
            report.add(outcome)
            deployed_stack.append(rel)

            _maybe_call_hooks(rel, "post", {"context": cfg.context})

        return report

    except Exception as e:
        # 4) Best-effort rollback of what we deployed in this session
        err_msg = str(e)
        # Mark current failing release (if we were in middle)
        if report.outcomes and report.outcomes[-1].status != "OK":
            report.outcomes[-1].error = err_msg

        for rel in reversed(deployed_stack):
            try:
                helm.uninstall(rel.name, rel.namespace)
                report.add(ReleaseOutcome(name=rel.name, namespace=rel.namespace, status="ROLLED_BACK"))
            except Exception as re:  # keep rolling even if a rollback fails
                report.add(ReleaseOutcome(name=rel.name, namespace=rel.namespace, status="FAILED", error=str(re)))

        # Also record the error on a final FAILED outcome if none added
        if not report.outcomes or report.outcomes[-1].status == "OK":
            report.add(ReleaseOutcome(name="__executor__", namespace="-", status="FAILED", error=err_msg))
        return report
