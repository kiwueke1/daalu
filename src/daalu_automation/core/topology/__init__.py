"""Live network-fabric topology — inventory + LLDP + telemetry.

Daalu renders the customer's switch/server fabric as a live graph:

* **nodes** come from the source of truth (Nautobot) — the intended inventory.
* **links** are reconciled from **observed LLDP neighbours** (what the devices
  actually see on the wire) against the **intended cabling** (Nautobot), and
  coloured by **telemetry** — a link the fabric can see and that is carrying
  state is green, an intended link that has gone missing is red, and a
  neighbour that disagrees with the cabling plan is amber (miscabling).

The whole thing degrades gracefully: with no reachable devices it still draws
the intended inventory (links neutral, never falsely red), so the UI is useful
before onboarding completes.
"""

from daalu_automation.core.topology.models import (
    TopologyGraph,
    TopologyLink,
    TopologyNode,
)
from daalu_automation.core.topology.service import build_topology

__all__ = [
    "TopologyGraph",
    "TopologyLink",
    "TopologyNode",
    "build_topology",
]
