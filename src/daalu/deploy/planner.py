# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections import deque
from typing import List, Dict, Set, Optional

from ..config.models import ClusterConfig, ReleaseSpec

# Observer bits
from ..observers.dispatcher import EventBus
from ..observers.events import PlanComputed, PlanFailed, new_ctx


class UnknownDependencyError(ValueError):
    pass


class CyclicDependencyError(ValueError):
    pass


def _validate_dependencies(cfg: ClusterConfig) -> None:
    names: Set[str] = {r.name for r in cfg.releases}
    for r in cfg.releases:
        for d in r.dependencies:
            if d not in names:
                raise UnknownDependencyError(
                    f"Release '{r.name}' depends on unknown release '{d}'"
                )


def plan(
    cfg: ClusterConfig,
    bus: Optional[EventBus] = None,
    run_ctx: Optional[dict] = None,
) -> List[ReleaseSpec]:
    """
    Stable topological sort of releases based on 'dependencies'.
    Emits PlanComputed / PlanFailed if an EventBus is provided.
    """
    ctx = run_ctx or new_ctx(env=cfg.environment, context=cfg.context)
    try:
        _validate_dependencies(cfg)

        by_name: Dict[str, ReleaseSpec] = cfg.by_name()
        indeg: Dict[str, int] = {r.name: 0 for r in cfg.releases}
        graph: Dict[str, Set[str]] = {r.name: set(r.dependencies) for r in cfg.releases}

        for r in cfg.releases:
            for d in r.dependencies:
                indeg[r.name] += 1

        queue = deque(sorted([n for n, deg in indeg.items() if deg == 0]))
        order: List[ReleaseSpec] = []

        while queue:
            n = queue.popleft()
            order.append(by_name[n])
            for m, deps in graph.items():
                if n in deps:
                    indeg[m] -= 1
                    if indeg[m] == 0:
                        queue.append(m)
                        queue = deque(sorted(queue))  # deterministic

        if len(order) != len(cfg.releases):
            raise CyclicDependencyError("Cyclic dependency detected among releases")

        if bus:
            bus.emit(PlanComputed(order=[r.name for r in order], **ctx))
        return order

    except Exception as e:
        if bus:
            bus.emit(PlanFailed(error=str(e), **ctx))
        raise
