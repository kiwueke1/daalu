# daalu/src/daalu/bootstrap/hosts_inventory.py
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import subprocess

try:
    from jinja2 import Environment, FileSystemLoader
except Exception:
    Environment = None  # optional dep; tests will skip template rendering if missing


def _kubectl_json(args: List[str], kube_context: Optional[str] = None, kubeconfig: Optional[str] = None) -> dict:
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    if kube_context:
        cmd += ["--context", kube_context]
    cmd += args + ["-o", "json"]
    cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr)
    return json.loads(cp.stdout or "{}")


def get_node_names(workload_kubeconfig: str) -> List[str]:
    data = _kubectl_json(["get", "nodes"], kubeconfig=workload_kubeconfig)
    return [item["metadata"]["name"] for item in data.get("items", [])]


def get_machine_internal_ip(mgmt_context: Optional[str], machine_name: str) -> Optional[str]:
    # Machines live on the management cluster; ask for a single Machine by name
    try:
        data = _kubectl_json(["get", "machines", machine_name], kube_context=mgmt_context)
    except RuntimeError:
        return None
    addrs = data.get("status", {}).get("addresses", [])
    for a in addrs:
        if a.get("type") == "InternalIP":
            return a.get("address")
    return None


def build_hosts_entries(
    mgmt_context: Optional[str],
    workload_kubeconfig: str,
) -> List[Tuple[str, str]]:
    """Return [(ip, hostname), ...] for each workload node, resolving IP via CAPI Machine on mgmt."""
    names = get_node_names(workload_kubeconfig)
    out: List[Tuple[str, str]] = []
    for n in names:
        ip = get_machine_internal_ip(mgmt_context, n)
        if ip:
            out.append((ip, n))
    return out


def update_hosts_file(
    entries: List[Tuple[str, str]],
    hosts_file: Path,
    domain_suffix: str,
    cleanup_regex: Optional[str] = None,
) -> None:
    text = hosts_file.read_text() if hosts_file.exists() else ""
    if cleanup_regex:
        text = "\n".join([ln for ln in text.splitlines() if not re.search(cleanup_regex, ln)])

    for ip, host in entries:
        fqdn = f"{host}.{domain_suffix}"
        line = f"{ip} {host} {fqdn}"
        # remove any existing line for this host
        text = "\n".join([ln for ln in text.splitlines() if not re.search(rf"\b{re.escape(host)}(\s|$)", ln)])
        text += ("\n" if text and not text.endswith("\n") else "") + line

    hosts_file.write_text(text + ("\n" if not text.endswith("\n") else ""))


def render_inventory_templates(
    entries: List[Tuple[str, str]],
    templates_dir: Path,
    output_hosts_ini: Path,
    output_openstack_hosts_ini: Path,
    extra_vars: Optional[Dict[str, str]] = None,
) -> None:
    if Environment is None:
        raise RuntimeError("jinja2 is required for inventory rendering. Add it to requirements if you need this.")
    env = Environment(loader=FileSystemLoader(str(templates_dir)))
    ctx = {
        "hosts_entries": [{"ip": ip, "hostname": hn} for ip, hn in entries],
    }
    if extra_vars:
        ctx.update(extra_vars)

    hosts_tpl = env.get_template("hosts.ini.j2")
    openstack_tpl = env.get_template("openstack_hosts.ini.j2")
    output_hosts_ini.write_text(hosts_tpl.render(**ctx))
    output_openstack_hosts_ini.write_text(openstack_tpl.render(**ctx))
