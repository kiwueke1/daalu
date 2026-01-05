# src/daalu/bootstrap/metal3/helpers.py

import time
import subprocess
import base64

from pathlib import Path
from daalu.utils.helpers import kubectl, run
from typing import Optional



def fetch_cluster_kubeconfig(
    *,
    cluster_name: str,
    namespace: str,
    out_path: Path,
) -> Path:
    """
    Fetch and write the workload cluster kubeconfig from the
    <cluster>-kubeconfig Secret.
    """
    secret_b64 = subprocess.check_output(
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
        text=True,
    )

    out_path.write_bytes(base64.b64decode(secret_b64))
    return out_path

def wait_for_pods_running(
    kubeconfig: Path,
    *,
    namespace: Optional[str] = None,
    retries: int = 150,
    delay: int = 20,
) -> None:
    selector = ["--all-namespaces"] if namespace is None else ["-n", namespace]

    for _ in range(retries):
        result = subprocess.check_output(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                "pods",
                *selector,
                "--field-selector",
                "status.phase!=Running",
                "--no-headers"
            ],
            text=True,
        ).strip()

        if not result:
            return
        
        time.sleep(delay)

    raise TimeoutError("Timed out waiting for pods to transition to Running.")

def wait_for_nodes_ready(
    kubeconfig: Path,
    *,
    expected_count: int,
    retries: int = 150,
    delay: int = 3,
) -> None:
    for _ in range(retries):
        out = subprocess.check_output(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                "nodes",
                "-o",
                "jsonpath={range .items[*]}{.status.conditions[?(@.type=='Ready')].status}{'\\n'}{end}",
            ],
            text=True,
        ).splitlines()

        if out.count("True") == expected_count:
            return

        time.sleep(delay)

    raise TimeoutError("Timed out waiting for Nodes to be Ready")


# -----------------------------
# CRD / pivot helpers
# -----------------------------

def label_crds_for_pivot(
    *,
    kubeconfig: Optional[Path] = None,
) -> None:
    labels = [
        "clusterctl.cluster.x-k8s.io=",
        "clusterctl.cluster.x-k8s.io/move=",
        "clusterctl.cluster.x-k8s.io/move-hierarchy=",
    ]

    for crd in ["baremetalhosts.metal3.io", "hardwaredata.metal3.io"]:
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
    context: str | None = None,
    timeout_seconds: int = 1800,
) -> None:

    start = time.time()

    while True:
        if time.time() - start > timeout_seconds:
            raise TimeoutError(
                f"Control plane for cluster {cluster_name} did not become ready"
            )

        cmd = [
            "kubectl",
            *(["--context", context] if context else []),
            "-n", namespace,
            "get", "kubeadmcontrolplane", cluster_name,
            "-o", "jsonpath={.status.ready}",
        ]

        ready = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        ).stdout.strip()

        if ready == "true":
            return

        time.sleep(10)
