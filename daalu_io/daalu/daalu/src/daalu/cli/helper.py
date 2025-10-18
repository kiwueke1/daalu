# src/daalu/cli/helper.py
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple

from daalu.bootstrap.node.models import NodeBootstrapPlan


def _default_workspace_root() -> Path:
    # Resolve from this file if WORKSPACE_ROOT not provided
    return Path(__file__).resolve().parents[3]


def inventory_path(workspace_root: Optional[Path] = None) -> Path:
    """
    Return the path to the rendered inventory created by SetupManager.
    Uses WORKSPACE_ROOT env var if set, otherwise resolves from this file.
    """
    root = workspace_root or Path(os.environ.get("WORKSPACE_ROOT", _default_workspace_root()))
    return root / "cloud-config" / "inventory" / "openstack_hosts.ini"

def read_hosts_from_inventory(inv_path: Path) -> List[Tuple[str, str]]:
    """
    Parse an INI-like hosts file with sections like [controllers], [computes], [cephs].
    Returns a list of (hostname, address), trimming FQDN suffix like '.net.daalu.io'.
    """
    hosts: List[Tuple[str, str]] = []
    if not inv_path.exists():
        print('no hosts found, testing')
        return hosts

    DOMAIN_SUFFIX = ".net.daalu.io"  # ✅ define suffix to remove

    in_section = False
    for raw in inv_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            in_section = section_name in ("controllers", "computes", "cephs")
            continue
        if not in_section:
            continue

        parts = line.split()
        if not parts:
            continue

        hname = parts[0]
        addr = None
        for p in parts[1:]:
            if p.startswith("ansible_host="):
                addr = p.split("=", 1)[1]
                break

        # ✅ Trim FQDN domain suffix if present
        if hname.endswith(DOMAIN_SUFFIX):
            hname = hname[: -len(DOMAIN_SUFFIX)]

        if hname and addr:
            hosts.append((hname, addr))

    return hosts

def read_hosts_from_inventory_1(inv_path: Path) -> List[Tuple[str, str]]:
    """
    Parse an INI-like hosts file with a section called [servers].
    Returns a list of (hostname, address).
    """
    hosts: List[Tuple[str, str]] = []
    if not inv_path.exists():
        print('no hosts found, testing')
        return hosts

    in_section = False
    for raw in inv_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            in_section = section_name in ("controllers", "computes", "cephs")  # ✅ Accept both
            continue
        if not in_section:
            continue

        parts = line.split()
        if not parts:
            continue
        hname = parts[0]
        addr = None
        for p in parts[1:]:
            if p.startswith("ansible_host="):
                addr = p.split("=", 1)[1]
                break
        if hname and addr:
            hosts.append((hname, addr))
    return hosts


def plan_from_tags(node_tags: Optional[str]) -> NodeBootstrapPlan:
    """
    Emulate Ansible tags. If tags is None/empty: run all roles.
    Else, turn off roles not listed.
    Supported tags: apparmor, netplan, ssh, inotify, istio
    """
    if not node_tags:
        return NodeBootstrapPlan()  # all True
    tags = {t.strip() for t in node_tags.split(",") if t.strip()}
    return NodeBootstrapPlan(
        run_apparmor=("apparmor" in tags),
        run_netplan=("netplan" in tags),
        run_ssh_and_hostname=("ssh" in tags),
        run_inotify_limits=("inotify" in tags),
        run_istio_modules=("istio" in tags),
    )


def maybe_read_kubeconfig_text(kubeconfig_path: Path = Path("/var/lib/tmp/kubeconfig")) -> Optional[str]:
    """
    Best-effort read of the workload kubeconfig produced during Setup.
    Returns the file content or None if missing/unreadable.
    """
    try:
        return kubeconfig_path.read_text()
    except Exception:
        return None

def read_group_from_inventory_1(inv_path: Path, group: str) -> List[Tuple[str, str]]:
    """
    Return [(hostname, ansible_host)] for a given inventory group, e.g. "cephs".
    """
    hosts: List[Tuple[str, str]] = []
    if not inv_path.exists():
        return hosts

    in_section = False
    header = f"[{group}]"
    for raw in inv_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_section = (line == header)
            continue
        if not in_section:
            continue

        parts = line.split()
        if not parts:
            continue
        hname = parts[0]
        addr = None
        for p in parts[1:]:
            if p.startswith("ansible_host="):
                addr = p.split("=", 1)[1]
                break
        if hname and addr:
            hosts.append((hname, addr))
    return hosts

def read_group_from_inventory(inv_path: Path, group: str) -> List[Tuple[str, str]]:
    """
    Return [(hostname, ansible_host)] for a given inventory group, e.g. "cephs".
    Trims out any FQDN domain suffix like '.net.daalu.io' from the hostname.
    """
    hosts: List[Tuple[str, str]] = []
    if not inv_path.exists():
        return hosts

    in_section = False
    header = f"[{group}]"

    for raw in inv_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_section = (line == header)
            continue
        if not in_section:
            continue

        parts = line.split()
        if not parts:
            continue

        hname = parts[0]
        addr = None
        for p in parts[1:]:
            if p.startswith("ansible_host="):
                addr = p.split("=", 1)[1]
                break

        # ✅ Trim domain suffix if present
        if hname.endswith(".net.daalu.io"):
            hname = hname.replace(".net.daalu.io", "")

        if hname and addr:
            hosts.append((hname, addr))

    return hosts
