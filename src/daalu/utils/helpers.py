# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/common/helpers.py

from __future__ import annotations
import subprocess
import time
from pathlib import Path
from typing import Optional
import yaml
import base64
from typing import Optional, Any
import json
import ipaddress

from daalu.execution.runner import CommandRunner
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
from daalu.bootstrap.openstack.endpoints import (
    OpenStackHelmEndpoints,
)

# from daalu.utils.helpers import kubectl, clusterctl

def run(
    cmd: list[str],
    *,
    env: Optional[dict] = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        env=env,
        capture_output=capture_output
    )

def kubectl(
    args: list[str],
    *,
    kubeconfig: Optional[Path] = None,
) -> None:
    env = None
    if kubeconfig:
        env = {"KUBECONFIG": str(kubeconfig)}
    run(["kubectl"] + args, env=env)

def wait_until(
    predicate,
    *,
    retries: int,
    delay: int,
    error: str,
):
    for _ in range(retries):
        if predicate():
            return
        time.sleep(delay)
    raise TimeoutError(error)

def clusterctl(args: list[str]) -> None:
    run(["clusterctl"], args)



def load_yaml_file(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"YAML file not found: {path}")
    with path.open() as f:
        return yaml.safe_load(f) or {}


def wait_for_node_interface_ipv4(
    kubeconfig: Path,
    *,
    ctx: Any,
    namespace: str = "openstack",
    node_selector: str = "openstack-control-plane=enabled",
    interface: str,
    debug_image: str = "ubuntu:24.04",
    retries: int = 120,
    delay: int = 5,
) -> None:
    """
    Wait until each selected node has an IPv4 address assigned on the given
    host interface.

    This replaces the old keepalived initContainer shell loop by doing an
    actual host-level check via:
      kubectl debug node/<node> --image=<debug_image> -- chroot /host ip -4 addr show dev <iface>

    Notes:
      - Requires 'kubectl debug' support and ability to create debug pods.
      - The debug image must contain 'chroot' and 'ip' (iproute2).
    """
    runner = CommandRunner(
        logger=getattr(ctx, "logger", None),
        dry_run=getattr(ctx, "dry_run", False),
        label="wait_for_node_interface_ipv4",
    )

    # 1) Get list of nodes we care about
    result = runner.run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "get",
            "nodes",
            "-l",
            node_selector,
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
        ],
        capture_output=True,
        check=True,
    )

    nodes = [n.strip() for n in (result.stdout or "").splitlines() if n.strip()]
    if not nodes:
        raise RuntimeError(
            f"[wait_for_node_interface_ipv4] No nodes matched selector '{node_selector}'"
        )

    def _has_ipv4_on_node(node: str) -> bool:
        # We do NOT use any shell script. We directly exec 'chroot /host ip ...'
        # via kubectl debug.
        #
        # '--quiet' keeps output minimal on newer kubectl; if your kubectl doesn't
        # support it, remove it.
        cmd = [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "debug",
            f"node/{node}",
            "--image",
            debug_image,
            "--",
            "chroot",
            "/host",
            "ip",
            "-4",
            "addr",
            "show",
            "dev",
            interface,
        ]

        rc = 0
        out = ""
        err = ""
        try:
            r = runner.run(cmd, capture_output=True, check=False)
            rc = getattr(r, "returncode", 0) or 0
            out = (r.stdout or "").strip()
            err = (r.stderr or "").strip()
        except Exception as e:
            # runner.run shouldn't normally throw with check=False, but keep it safe.
            if runner.logger:
                runner.logger.warning(
                    f"[wait_for_node_interface_ipv4] debug failed on {node}: {e}"
                )
            return False

        if rc != 0:
            # Often indicates RBAC, debug disabled, image pull failure, etc.
            # Keep retrying; final timeout will surface as an error with guidance.
            if runner.logger:
                runner.logger.info(
                    f"[wait_for_node_interface_ipv4] debug rc={rc} node={node} err={err}"
                )
            return False

        # ip output includes "inet X.Y.Z.W/.." when an IPv4 is present
        return " inet " in f" {out} "

    # 2) Wait for each node to satisfy condition
    for node in nodes:
        for attempt in range(1, retries + 1):
            ok = _has_ipv4_on_node(node)

            if attempt == 1 or attempt % 10 == 0:
                msg = (
                    f"[wait_for_node_interface_ipv4] node={node} iface={interface} "
                    f"attempt={attempt}/{retries} ok={ok}"
                )
                log.debug(msg)
                if runner.logger:
                    runner.logger.info(msg)

            if ok:
                break

            time.sleep(delay)
        else:
            raise TimeoutError(
                f"Timed out waiting for IPv4 on interface '{interface}' on node '{node}'. "
                f"Check kubectl debug permissions/support and that '{interface}' exists on the host."
            )

    success = (
        f"[wait_for_node_interface_ipv4] SUCCESS: IPv4 present on '{interface}' "
        f"for nodes selector '{node_selector}'"
    )
    log.debug(success)
    if runner.logger:
        runner.logger.info(success)

def fetch_cluster_kubeconfig(
    *,
    cluster_name: str,
    namespace: str,
    out_path: Path,
    ctx: Any,
) -> Path:
    """
    Fetch and write the workload cluster kubeconfig from the
    <cluster>-kubeconfig Secret.
    """
    runner = CommandRunner(
        logger=getattr(ctx, "logger", None),
        dry_run=getattr(ctx, "dry_run", False),
        label="fetch_cluster_kubeconfig",
    )

    result = runner.run(
        [
            "kubectl",
            "get",
            "secret",
            f"{cluster_name}-kubeconfig",
            "-n",
            namespace,
            "-o",
            "jsonpath={.data.value}",
        ],
        capture_output=True,
        check=True,
    )

    out_path.write_bytes(base64.b64decode(result.stdout or ""))
    return out_path


def wait_for_pods_running(
    kubeconfig: Path,
    *,
    ctx: Any,
    namespace: Optional[str] = None,
    retries: int = 150,
    delay: int = 20,
) -> None:
    runner = CommandRunner(
        logger=getattr(ctx, "logger", None),
        dry_run=getattr(ctx, "dry_run", False),
        label="wait_for_pods_running",
    )

    selector = ["--all-namespaces"] if namespace is None else ["-n", namespace]

    for _ in range(retries):
        result = runner.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                "pods",
                *selector,
                "--field-selector",
                "status.phase!=Running",
                "--no-headers",
            ],
            capture_output=True,
            check=True,
        )

        if not (result.stdout or "").strip():
            return

        time.sleep(delay)

    raise TimeoutError("Timed out waiting for pods to transition to Running.")


def wait_for_nodes_ready(
    kubeconfig: Path,
    *,
    ctx: Any,
    expected_count: int,
    retries: int = 150,
    delay: int = 3,
) -> None:
    runner = CommandRunner(
        logger=getattr(ctx, "logger", None),
        dry_run=getattr(ctx, "dry_run", False),
        label="wait_for_nodes_ready",
    )

    for attempt in range(1, retries + 1):
        # Get readiness status
        result = runner.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                "nodes",
                "-o",
                "jsonpath={range .items[*]}{.metadata.name}{' '}"
                "{.status.conditions[?(@.type=='Ready')].status}{'\\n'}{end}",
            ],
            capture_output=True,
            check=True,
        )

        lines = (result.stdout or "").strip().splitlines()
        ready = sum(1 for l in lines if l.endswith(" True"))

        # Periodic progress output
        if attempt == 1 or attempt % 10 == 0:
            msg = (
                f"[wait_for_nodes_ready] Attempt {attempt}/{retries}: "
                f"{ready}/{expected_count} nodes Ready"
            )
            log.debug(msg)
            if runner.logger:
                runner.logger.info(msg)

            # Show current node state
            nodes_out = runner.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(kubeconfig),
                    "get",
                    "nodes",
                    "-o",
                    "wide",
                ],
                capture_output=True,
                check=True,
            ).stdout

            log.debug(nodes_out)
            if runner.logger:
                runner.logger.info("\n" + nodes_out)

        if ready == expected_count:
            success_msg = (
                f"[wait_for_nodes_ready] SUCCESS: "
                f"All {expected_count} nodes are Ready"
            )
            log.debug(success_msg)
            if runner.logger:
                runner.logger.info(success_msg)

            final_nodes = runner.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(kubeconfig),
                    "get",
                    "nodes",
                    "-o",
                    "wide",
                ],
                capture_output=True,
                check=True,
            ).stdout

            log.debug(final_nodes)
            if runner.logger:
                runner.logger.info("\n" + final_nodes)

            return

        time.sleep(delay)

    raise TimeoutError(
        f"Timed out waiting for {expected_count} nodes to be Ready"
    )

# ---------------------------------------------------------------------
# CRD / pivot helpers
# ---------------------------------------------------------------------

def label_crds_for_pivot(
    *,
    kubeconfig: Optional[Path] = None,
) -> None:
    labels = [
        "clusterctl.cluster.x-k8s.io=",
        "clusterctl.cluster.x-k8s.io/move=",
        "clusterctl.cluster.x-k8s.io/move-hierarchy=",
    ]

    for crd in [
        "baremetalhosts.metal3.io",
        "hardwaredata.metal3.io",
    ]:
        kubectl(
            ["label", "crds", crd, *labels, "--overwrite"],
            kubeconfig=kubeconfig,
        )


def move_cluster_objects(
    *,
    from_kubeconfig: Optional[Path],
    to_kubeconfig: Path,
    namespace: str,
    verbose: bool = True,
) -> None:
    args = ["move", "--to-kubeconfig", str(to_kubeconfig), "-n", namespace]
    if verbose:
        args += ["-v", "10"]

    clusterctl(args)


def wait_for_control_plane_ready(
    *,
    cluster_name: str,
    namespace: str,
    ctx: Any,
    context: str | None = None,
    timeout_seconds: int = 1800,
) -> None:
    runner = CommandRunner(
        logger=getattr(ctx, "logger", None),
        dry_run=getattr(ctx, "dry_run", False),
        label="wait_for_control_plane_ready",
    )

    start = time.time()

    while True:
        if time.time() - start > timeout_seconds:
            raise TimeoutError(
                f"Control plane for cluster {cluster_name} did not become ready"
            )

        cmd = [
            "kubectl",
            *(["--context", context] if context else []),
            "-n",
            namespace,
            "get",
            "kubeadmcontrolplane",
            cluster_name,
            "-o",
            "jsonpath={.status.ready}",
        ]

        result = runner.run(
            cmd,
            capture_output=True,
            check=True,
        )

        if (result.stdout or "").strip() == "true":
            return

        time.sleep(10)


def deploy_cni(
    kubeconfig: Path,
    *,
    ctx: Any,
    cni: str = "cilium",
) -> None:
    runner = CommandRunner(
        logger=getattr(ctx, "logger", None),
        dry_run=getattr(ctx, "dry_run", False),
        label="deploy_cni",
    )

    if cni != "cilium":
        raise ValueError(f"Unsupported CNI: {cni}")

    # 1) Get control-plane IP (InternalIP)
    log.debug("starting cilium install from deploy_cni method")
    result = runner.run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "get",
            "nodes",
            "-l",
            "node-role.kubernetes.io/control-plane",
            "-o",
            "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}",
        ],
        capture_output=True,
        check=True,
    )

    #control_plane_ip = (result.stdout or "").strip()
    
    control_plane_ip = "10.10.0.249"
    if not control_plane_ip:
        raise RuntimeError("Failed to determine control plane IP for CNI install")

    # 2) Add Cilium repo
    runner.run(
        ["helm", "repo", "add", "cilium", "https://helm.cilium.io/"],
        check=True,
    )
    runner.run(["helm", "repo", "update"], check=True)

    # 3) Install Cilium
    runner.run(
        [
            "helm",
            "upgrade",
            "--install",
            "cilium",
            "cilium/cilium",
            "--namespace",
            "kube-system",
            "--create-namespace",
            "--set",
            "ipam.mode=kubernetes",
            "--set",
            "kubeProxyReplacement=true",
            "--set",
            f"k8sServiceHost={control_plane_ip}",
            "--set",
            "k8sServicePort=6443",
            "--set",
            "operator.replicas=1",
            "--set",
            "hubble.enabled=true",
            "--set",
            "hubble.relay.enabled=true",
            "--set",
            "hubble.ui.enabled=true",
            "--kubeconfig",
            str(kubeconfig),
        ],
        check=True,
    )

def wait_for_cni_ready(
    kubeconfig: Path,
    *,
    ctx: Any,
    retries: int = 60,
    delay: int = 10,
) -> None:
    runner = CommandRunner(
        logger=getattr(ctx, "logger", None),
        dry_run=getattr(ctx, "dry_run", False),
        label="wait_for_cni_ready",
    )

    for attempt in range(1, retries + 1):
        result = runner.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                "pods",
                "-n",
                "kube-system",
                "-l",
                "k8s-app=cilium",
                "-o",
                "jsonpath={range .items[*]}{.metadata.name}{' '}{.status.phase}{'\\n'}{end}",
            ],
            capture_output=True,
            check=True,
        )

        lines = (result.stdout or "").strip().splitlines()
        total = len(lines)
        running = sum(1 for l in lines if l.endswith(" Running"))

        msg = (
            f"[wait_for_cni_ready] Attempt {attempt}/{retries}: "
            f"{running}/{total} Cilium pods Running"
        )

        log.debug(msg)
        if runner.logger:
            runner.logger.info(msg)

        # Periodically show full pod status for visibility
        if attempt == 1 or attempt % 5 == 0:
            pods_out = runner.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(kubeconfig),
                    "get",
                    "pods",
                    "-n",
                    "kube-system",
                    "-l",
                    "k8s-app=cilium",
                    "-o",
                    "wide",
                ],
                capture_output=True,
                check=True,
            ).stdout

            log.debug(pods_out)
            if runner.logger:
                runner.logger.info("\n" + pods_out)

        if total > 0 and running == total:
            success_msg = "[wait_for_cni_ready] SUCCESS: All Cilium pods are Running"
            log.debug(success_msg)
            if runner.logger:
                runner.logger.info(success_msg)
            return

        time.sleep(delay)

    raise TimeoutError("Timed out waiting for Cilium pods to become Ready")



CONTROL_PLANE_LABELS = (
    "node-role.kubernetes.io/control-plane",
    "node-role.kubernetes.io/master",  # for older clusters
)


def update_hosts_and_inventory_old(
    *,
    kubeconfig: Path,
    workspace_root: Path,
    domain_suffix: str,
    ctx: Any,
) -> None:
    import json
    import ipaddress
    from pathlib import Path

    log.debug("Updating hosts and inventory...")

    runner = CommandRunner(
        logger=getattr(ctx, "logger", None),
        dry_run=getattr(ctx, "dry_run", False),
    )

    # Pull full JSON so we can reliably detect role-label presence
    result = runner.run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "get",
            "nodes",
            "-o",
            "json",
        ],
        capture_output=True,
        check=True,
    )

    data = json.loads(result.stdout or "{}")
    items = data.get("items", [])

    controllers: list[str] = []
    computes: list[str] = []
    ceph: list[str] = []

    # Allocate secondary interface IPs (int2) starting at 10.44.0.11
    int2_network = ipaddress.IPv4Network("10.44.0.0/24")
    int2_iter = iter(int2_network.hosts())
    for _ in range(10):  # Skip .1 → .10
        next(int2_iter)

    hosts_path = Path("/etc/hosts")
    existing_lines = hosts_path.read_text(encoding="utf-8").splitlines()

    def clean_hosts(
        lines: list[str],
        *,
        ip: str,
        hostname: str,
        fqdn: str,
    ) -> list[str]:
        """Remove any line that conflicts with this node."""
        cleaned: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                cleaned.append(line)
                continue

            parts = stripped.split()
            line_ip = parts[0]
            names = parts[1:]

            # 🔥 Remove if IP matches OR hostname/FQDN matches
            if (
                line_ip == ip
                or hostname in names
                or fqdn in names
            ):
                continue

            cleaned.append(line)

        return cleaned

    new_hosts_lines = list(existing_lines)

    for node in items:
        name = node["metadata"]["name"]
        labels = node["metadata"].get("labels", {})

        # Find InternalIP
        ip = None
        for addr in node.get("status", {}).get("addresses", []):
            if addr.get("type") == "InternalIP":
                ip = addr.get("address")
                break
        if not ip:
            raise RuntimeError(f"Node {name} has no InternalIP")

        fqdn = f"{name}.{domain_suffix}"
        int2_ip = str(next(int2_iter))

        entry = f"{fqdn} ansible_host={ip} int2_ip={int2_ip}"
        ceph.append(entry)

        is_control_plane = any(k in labels for k in CONTROL_PLANE_LABELS)
        if is_control_plane:
            controllers.append(entry)
        else:
            computes.append(entry)

        # 🔥 HARD CLEAN: remove all conflicting entries
        new_hosts_lines = clean_hosts(
            new_hosts_lines,
            ip=ip,
            hostname=name,
            fqdn=fqdn,
        )

        # Append canonical entry
        new_hosts_lines.append(f"{ip} {name} {fqdn}")

    # Write /etc/hosts atomically
    tmp_hosts = hosts_path.with_suffix(".tmp")
    tmp_hosts.write_text("\n".join(new_hosts_lines) + "\n", encoding="utf-8")

    runner.run(
        ["sudo", "cp", str(tmp_hosts), str(hosts_path)],
        check=True,
    )

    inventory = workspace_root / "cloud-config/inventory/openstack_hosts.ini"
    inventory.parent.mkdir(parents=True, exist_ok=True)

    inventory.write_text(
        "[controllers]\n\n"
        + "\n".join(controllers)
        + "\n\n[computes]\n\n"
        + "\n".join(computes)
        + "\n\n[ceph]\n\n"
        + "\n".join(ceph)
        + "\n",
        encoding="utf-8",
    )

def update_hosts_and_inventory(
    *,
    kubeconfig: Path,
    workspace_root: Path,
    domain_suffix: str,
    ctx: Any,
) -> None:
    import json
    import ipaddress
    from pathlib import Path

    log.debug("Updating hosts and inventory...")

    runner = CommandRunner(
        logger=getattr(ctx, "logger", None),
        dry_run=getattr(ctx, "dry_run", False),
        label="update_hosts_and_inventory",
    )

    # Pull full JSON so we can reliably detect role-label presence
    result = runner.run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "get",
            "nodes",
            "-o",
            "json",
        ],
        capture_output=True,
        check=True,
    )

    data = json.loads(result.stdout or "{}")
    items = data.get("items", [])

    controllers: list[str] = []
    computes: list[str] = []
    ceph: list[str] = []

    # Allocate secondary interface IPs (int2) starting at 10.44.0.11
    int2_network = ipaddress.IPv4Network("10.44.0.0/24")
    int2_iter = iter(int2_network.hosts())
    for _ in range(10):  # Skip .1 → .10
        next(int2_iter)

    hosts_path = Path("/etc/hosts")
    existing_lines = hosts_path.read_text(encoding="utf-8").splitlines()

    new_hosts_lines = list(existing_lines)

    def _remove_node_entries(
        lines: list[str],
        *,
        ip: str,
        name: str,
        fqdn: str,
    ) -> list[str]:
        """Remove any /etc/hosts lines that conflict with this node."""
        filtered: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                filtered.append(line)
                continue

            parts = stripped.split()
            line_ip = parts[0]
            names = parts[1:]

            # 🔥 Remove if IP OR hostname OR FQDN matches
            if (
                line_ip == ip
                or name in names
                or fqdn in names
            ):
                continue

            filtered.append(line)

        return filtered

    for node in items:
        name = node["metadata"]["name"]
        labels = node["metadata"].get("labels", {})

        # Find InternalIP
        ip = None
        for addr in node.get("status", {}).get("addresses", []):
            if addr.get("type") == "InternalIP":
                ip = addr.get("address")
                break
        if not ip:
            raise RuntimeError(f"Node {name} has no InternalIP")

        fqdn = f"{name}.{domain_suffix}"
        int2_ip = str(next(int2_iter))

        entry = f"{fqdn} ansible_host={ip} int2_ip={int2_ip}"
        ceph.append(entry)

        is_control_plane = any(k in labels for k in CONTROL_PLANE_LABELS)
        if is_control_plane:
            controllers.append(entry)
        else:
            computes.append(entry)

        # 🔥 HARD CLEAN: remove all conflicting entries
        new_hosts_lines = _remove_node_entries(
            new_hosts_lines,
            ip=ip,
            name=name,
            fqdn=fqdn,
        )

        # Append canonical entry
        new_hosts_lines.append(f"{ip} {name} {fqdn}")

    # Write updated hosts file to a user-writable temp location
    tmp_hosts = Path("/tmp/hosts.tmp")
    tmp_hosts.write_text("\n".join(new_hosts_lines) + "\n", encoding="utf-8")

    # Move into place with sudo (atomic replace)
    runner.run(
        ["sudo", "install", "-m", "0644", str(tmp_hosts), str(hosts_path)],
        check=True,
    )

    try:
        tmp_hosts.unlink(missing_ok=True)
    except Exception:
        pass

    inventory = workspace_root / "cloud-config/inventory/openstack_hosts.ini"
    inventory.parent.mkdir(parents=True, exist_ok=True)

    inventory.write_text(
        "[controllers]\n\n"
        + "\n".join(controllers)
        + "\n\n[computes]\n\n"
        + "\n".join(computes)
        + "\n\n[ceph]\n\n"
        + "\n".join(ceph)
        + "\n",
        encoding="utf-8",
    )



def update_hosts_and_inventory_1(
    *,
    kubeconfig: Path,
    workspace_root: Path,
    domain_suffix: str,
    ctx: Any,
) -> None:
    import json
    import ipaddress
    from pathlib import Path

    log.debug("Updating hosts and inventory...")

    runner = CommandRunner(
        logger=getattr(ctx, "logger", None),
        dry_run=getattr(ctx, "dry_run", False),
        label="update_hosts_and_inventory",
    )

    # Pull full JSON so we can reliably detect role-label presence
    result = runner.run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "get",
            "nodes",
            "-o",
            "json",
        ],
        capture_output=True,
        check=True,
    )

    data = json.loads(result.stdout or "{}")
    items = data.get("items", [])

    controllers: list[str] = []
    computes: list[str] = []
    ceph: list[str] = []

    # Allocate secondary interface IPs (int2) starting at 10.44.0.11
    int2_network = ipaddress.IPv4Network("10.44.0.0/24")
    int2_iter = iter(int2_network.hosts())
    for _ in range(10):  # Skip .1 → .10
        next(int2_iter)

    hosts_path = Path("/etc/hosts")
    existing_lines = hosts_path.read_text(encoding="utf-8").splitlines()

    # We will rebuild /etc/hosts by filtering + appending
    new_hosts_lines = list(existing_lines)

    def _remove_node_entries(lines: list[str], name: str, fqdn: str) -> list[str]:
        """Remove any /etc/hosts lines that reference this node."""
        filtered: list[str] = []
        for line in lines:
            if not line.strip() or line.strip().startswith("#"):
                filtered.append(line)
                continue

            parts = line.split()
            # Remove lines containing hostname or FQDN
            if name in parts or fqdn in parts:
                continue

            filtered.append(line)
        return filtered

    for node in items:
        name = node["metadata"]["name"]
        labels = node["metadata"].get("labels", {})

        # Find InternalIP
        ip = None
        for addr in node.get("status", {}).get("addresses", []):
            if addr.get("type") == "InternalIP":
                ip = addr.get("address")
                break
        if not ip:
            raise RuntimeError(f"Node {name} has no InternalIP")

        fqdn = f"{name}.{domain_suffix}"
        int2_ip = str(next(int2_iter))

        entry = f"{fqdn} ansible_host={ip} int2_ip={int2_ip}"
        ceph.append(entry)

        is_control_plane = any(k in labels for k in CONTROL_PLANE_LABELS)
        if is_control_plane:
            controllers.append(entry)
        else:
            computes.append(entry)

        # 🔥 FIX: replace existing entries for THIS NODE (not IP-based)
        new_hosts_lines = _remove_node_entries(new_hosts_lines, name, fqdn)

        # Append fresh correct entry
        new_hosts_lines.append(f"{ip} {name} {fqdn}")

    # Write updated /etc/hosts atomically
    #tmp_hosts = hosts_path.with_suffix(".tmp")
    #tmp_hosts.write_text("\n".join(new_hosts_lines) + "\n", encoding="utf-8")

    #runner.run(
    #    ["sudo", "cp", str(tmp_hosts), str(hosts_path)],
    #    check=True,
    #)
    # Write updated hosts file to a user-writable temp location
    tmp_hosts = Path("/tmp/hosts.tmp")
    tmp_hosts.write_text("\n".join(new_hosts_lines) + "\n", encoding="utf-8")

    # Move into place with sudo (atomic replace)
    runner.run(
        ["sudo", "install", "-m", "0644", str(tmp_hosts), str(hosts_path)],
        check=True,
    )

    # Optional cleanup
    try:
        tmp_hosts.unlink(missing_ok=True)
    except Exception:
        pass



    inventory = workspace_root / "cloud-config/inventory/openstack_hosts.ini"
    inventory.parent.mkdir(parents=True, exist_ok=True)

    inventory.write_text(
        "[controllers]\n\n"
        + "\n".join(controllers)
        + "\n\n[computes]\n\n"
        + "\n".join(computes)
        + "\n\n[ceph]\n\n"
        + "\n".join(ceph)
        + "\n",
        encoding="utf-8",
    )



def label_and_taint_nodes(self, opts: SetupOptions, entries: List[Tuple[str, str]]) -> None:
    for _, hostname in entries:
        self.runner.run(
            [
                "kubectl",
                "--kubeconfig",
                str(opts.workload_kubeconfig),
                "label",
                "node",
                hostname,
                "node.cilium.io/agent-not-ready=true",
                "kubernetes.io/os=linux",
                "--overwrite",
            ],
            check=False,
        )

        self.runner.run(
            [
                "kubectl",
                "--kubeconfig",
                str(opts.workload_kubeconfig),
                "taint",
                "node",
                hostname,
                "node.cilium.io/agent-not-ready=true:NoSchedule",
                "--overwrite",
            ],
            check=False,
        )




from pathlib import Path
from typing import Any

from daalu.bootstrap.openstack.endpoints import (
    OpenStackHelmEndpoints,
    OpenStackHelmEndpointsConfig,
)
from daalu.bootstrap.openstack.secrets_manager import SecretsManager
import logging

log = logging.getLogger("daalu")


def build_openstack_endpoints(
    *,
    kubectl,
    secrets_path: Path,
    namespace: str,
    region_name: str,
    keystone_public_host: str,
    service: str,
) -> dict[str, Any]:
    """
    Daalu replacement for roles/openstack_helm_endpoints

    Builds fully-resolved OpenStack Helm `endpoints:` values by:
      - Loading inventory secrets.yaml
      - Ensuring inventory-backed K8s secrets exist
      - Reading operator-generated secrets (Percona, RabbitMQ)
      - Validating all required credentials
      - Returning a complete, non-null endpoints block

    This mirrors Atmosphere behavior and MUST be used by all OpenStack components.
    """

    # -------------------------------------------------
    # 1) Load inventory secrets.yaml
    # -------------------------------------------------
    secrets = SecretsManager.from_yaml(
        path=secrets_path,
        namespace=namespace,
    )

    # -------------------------------------------------
    # 2) Ensure inventory-backed K8s Secrets exist
    #    (does NOT read operator secrets)
    # -------------------------------------------------
    secrets.ensure_k8s_secrets(kubectl)

    # -------------------------------------------------
    # 3) Build operator-aware endpoints
    #    (Percona + RabbitMQ + service credentials)
    # -------------------------------------------------
    cfg = OpenStackHelmEndpointsConfig(
        namespace=namespace,
        region_name=region_name,
        keystone_public_host=keystone_public_host,
    )

    endpoints_builder = OpenStackHelmEndpoints(
        cfg=cfg,
        secrets=secrets,
    )

    return endpoints_builder.build_common_endpoints(
        kubectl=kubectl,
        service=service,
        keystone_api_service="keystone-api",
    )
