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




