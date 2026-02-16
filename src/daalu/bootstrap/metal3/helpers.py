# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/metal3/helpers.py

from __future__ import annotations

import logging
import time
import base64
from pathlib import Path
from typing import Optional, Any
import json
import ipaddress

from daalu.execution.runner import CommandRunner
from daalu.utils.helpers import kubectl, clusterctl

log = logging.getLogger("daalu")


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

    cmd = [
        "kubectl",
        "get",
        "secret",
        f"{cluster_name}-kubeconfig",
        "-n",
        namespace,
        "-o",
        "jsonpath={.data.value}",
    ]

    retries = 30
    delay = 20
    for attempt in range(1, retries + 1):
        result = runner.run(cmd, capture_output=True, check=False)
        if result.returncode == 0 and (result.stdout or "").strip():
            break
        log.info(
            "Waiting for kubeconfig secret to be ready (attempt %d/%d)...",
            attempt, retries,
        )
        print(
            f"  Waiting for kubeconfig secret '{cluster_name}-kubeconfig' "
            f"to be ready ({attempt}/{retries}, retrying in {delay}s)..."
        )
        time.sleep(delay)
    else:
        raise RuntimeError(
            f"Kubeconfig secret '{cluster_name}-kubeconfig' not available "
            f"after {retries * delay}s"
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

    # 1) Get control-plane IP (InternalIP) — wait for node to be available
    log.debug("starting cilium install from deploy_cni method")
    cp_cmd = [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "get",
        "nodes",
        "-l",
        "node-role.kubernetes.io/control-plane",
        "-o",
        "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}",
    ]

    retries = 60
    delay = 20
    control_plane_ip = ""
    for attempt in range(1, retries + 1):
        result = runner.run(cp_cmd, capture_output=True, check=False)
        control_plane_ip = (result.stdout or "").strip()
        if result.returncode == 0 and control_plane_ip:
            break
        log.info(
            "Waiting for control-plane node to be available (attempt %d/%d)...",
            attempt, retries,
        )
        print(
            f"  Waiting for control-plane node to be available "
            f"({attempt}/{retries}, retrying in {delay}s)..."
        )
        time.sleep(delay)

    if not control_plane_ip:
        raise RuntimeError(
            f"Failed to determine control plane IP after {retries * delay}s"
        )

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
