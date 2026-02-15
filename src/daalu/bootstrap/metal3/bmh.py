# src/daalu/bootstrap/metal3/bmh.py
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import List, Optional

@dataclass(frozen=True)
class BareMetalHostSummary:
    name: str
    namespace: str
    raw: dict

def _run(cmd: List[str]) -> str:
    p = subprocess.run(cmd, check=True, text=True, capture_output=True)
    return p.stdout

def list_bmhs(namespace: str, kube_context: Optional[str] = None) -> List[BareMetalHostSummary]:
    cmd = ["kubectl"]
    if kube_context:
        cmd += ["--context", kube_context]
    cmd += ["get", "baremetalhosts.metal3.io", "-n", namespace, "-o", "json"]

    data = json.loads(_run(cmd))
    items = data.get("items", [])
    return [
        BareMetalHostSummary(
            name=item["metadata"]["name"],
            namespace=item["metadata"]["namespace"],
            raw=item,
        )
        for item in items
    ]


def extract_bmh_nic_names(bmh: dict) -> List[str]:
    """
    Exact port of the Ansible filter `bmh_nic_names`.

    Equivalent to:
        sorted(set(nic["name"] for nic in bmh["status"]["hardware"]["nics"]))
    """
    nics = bmh["status"]["hardware"]["nics"]
    return sorted({nic["name"] for nic in nics})



def get_node_nics_from_cfg(cfg, node_name: str) -> list[str]:
    """
    Return NIC names for a node from cluster.yaml (source of truth).

    node_name must match BareMetalHost.metadata.name
    """
    metal3 = cfg.cluster_api.metal3
    if not metal3:
        raise RuntimeError(
            "cluster_api.metal3 is not defined in cluster.yaml "
            "(required when provider=metal3)"
        )

    nodes = metal3.nodes
    if not nodes:
        raise RuntimeError(
            "cluster_api.metal3.nodes is empty or not defined "
            "(required for Metal3 deployments)"
        )

    for node in nodes:
        if node.name == node_name:
            if not node.nics:
                raise RuntimeError(
                    f"No NICs defined for node '{node_name}' "
                    "in cluster.yaml"
                )
            return list(node.nics)

    raise RuntimeError(
        f"Node '{node_name}' not found in cluster_api.metal3.nodes "
        "(must match BareMetalHost name)"
    )
