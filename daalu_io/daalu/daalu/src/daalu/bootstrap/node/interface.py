# daalu/src/daalu/bootstrap/node/interface.py

from __future__ import annotations
from typing import Protocol, List
from .models import Host, NodeBootstrapPlan, NodeBootstrapOptions

class NodeBootstrapper(Protocol):
    """
    Contract for applying node-level OS configuration via SSH.
    Implementations should be idempotent and safe to re-run.
    """

    def bootstrap(self, hosts: List[Host], plan: NodeBootstrapPlan, opts: NodeBootstrapOptions) -> None:
        """
        Execute the requested roles on the provided hosts.
        Must raise on hard errors and continue on soft/idempotent cases.
        """
        ...
