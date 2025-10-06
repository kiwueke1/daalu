# src/daalu/deploy/planner.py
from __future__ import annotations

from collections import deque
from typing import List, Dict, Set

from ..config.models import ClusterConfig, ReleaseSpec


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


def plan(cfg: ClusterConfig) -> List[ReleaseSpec]:
    """
    Perform a stable topological sort of releases based on their 'dependencies'.
    - Validates that every dependency points to an existing release.
    - Deterministic order for equal indegree by sorting names.

    Returns:
        Ordered list of ReleaseSpec in the order they should be deployed.
    """
    _validate_dependencies(cfg)

    by_name: Dict[str, ReleaseSpec] = cfg.by_name()
    indeg: Dict[str, int] = {r.name: 0 for r in cfg.releases}
    graph: Dict[str, Set[str]] = {r.name: set(r.dependencies) for r in cfg.releases}

    # compute indegree
    for r in cfg.releases:
        for d in r.dependencies:
            indeg[r.name] += 1

    # start with all zero indegree nodes, sorted for determinism
    queue = deque(sorted([n for n, deg in indeg.items() if deg == 0]))
    order: List[ReleaseSpec] = []

    while queue:
        n = queue.popleft()
        order.append(by_name[n])
        # For each node that depends on n, drop its indegree
        for m, deps in graph.items():
            if n in deps:
                indeg[m] -= 1
                # When indegree becomes zero, put into queue, keeping it sorted
                if indeg[m] == 0:
                    # Insert in sorted position:
                    # Using simple append + sort for clarity (small list).
                    queue.append(m)
                    queue = deque(sorted(queue))

    if len(order) != len(cfg.releases):
        # There is at least one cycle
        raise CyclicDependencyError("Cyclic dependency detected among releases")

    return order
