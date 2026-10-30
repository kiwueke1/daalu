"""Pydantic contracts for the live topology graph (API + service share these)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Fabric tiers, ordered top→bottom for layout. "gateway" catches border/mgmt
# leaves; "unknown" is anything the role/platform heuristic can't place.
NodeTier = Literal["spine", "leaf", "server", "gateway", "unknown"]

# up       — observed on the wire (LLDP) and/or telemetry says the link is live
# down     — the SoT says this link should exist but it isn't observed
# mismatch — a neighbour is observed that disagrees with the intended cabling
# unknown  — no observation available (device unreachable / not probed) — the UI
#            draws it neutral-grey, NEVER red, so a probe outage can't look like
#            an outage of the fabric.
LinkStatus = Literal["up", "down", "mismatch", "mgmt", "unknown"]


class TopologyNode(BaseModel):
    id: str
    name: str
    tier: NodeTier = "unknown"
    platform: str = ""
    transport: str = ""
    primary_ip: str | None = None
    tags: list[str] = Field(default_factory=list)
    # Live signals (None = not probed / not applicable).
    reachable: bool | None = None
    lldp_ok: bool | None = None
    # True when the device transport supports the in-browser SSH terminal.
    can_terminal: bool = False
    note: str = ""


class TopologyLink(BaseModel):
    # Stable id derived from the two sorted endpoints so the same physical link
    # keeps its identity across polls (and React keys stay stable).
    id: str
    source: str
    source_if: str | None = None
    target: str
    target_if: str | None = None
    status: LinkStatus = "unknown"
    # Which side(s) reported the neighbour over LLDP — a one-sided sighting is
    # still "up" (LLDP can lag on one end during boot) but the UI can flag it.
    source_observed: bool = False
    target_observed: bool = False
    # True when Nautobot's cabling plan declares this link (the baseline that
    # lets us call a *missing* link "down" rather than merely "not seen").
    intended: bool = False
    detail: str = ""


class TopologyGraph(BaseModel):
    nodes: list[TopologyNode] = Field(default_factory=list)
    links: list[TopologyLink] = Field(default_factory=list)
    generated_at: str
    # True when at least one live source (LLDP or telemetry) contributed. When
    # False the graph is inventory-only and `degraded` explains why.
    observed: bool = False
    degraded: str | None = None
    # Counts for the header ("12 links · 11 up · 1 down").
    stats: dict[str, int] = Field(default_factory=dict)
