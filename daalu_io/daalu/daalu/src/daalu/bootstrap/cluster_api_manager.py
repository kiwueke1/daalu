# src/daalu/bootstrap/cluster_api_manager.py
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional

class ClusterAPIManager:
    """
    Applies Cluster API manifests on the MANAGEMENT cluster and waits until the
    workload control-plane is ready.
    """

    def __init__(self, repo_root: Path, mgmt_context: Optional[str] = None, kubeconfig: Optional[str] = None):
        self.repo_root = repo_root
        self.mgmt_context = mgmt_context
        self.kubeconfig = kubeconfig
        self.base_dir = repo_root / "playbooks"
        self.cluster_api_dir = self.base_dir / "cluster-api"

    def _kubectl(self) -> list[str]:
        cmd = ["kubectl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        if self.mgmt_context:
            cmd += ["--context", self.mgmt_context]
        return cmd

    def _clusterctl(self) -> list[str]:
        cmd = ["clusterctl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        # clusterctl does not take --context directly; kubeconfig+current-context is used
        return cmd

    def deploy(
        self,
        cluster_name: str = "openstack-infra",
        namespace: str = "default",
        timeout: int = 1800,
        interval: int = 30,
        secret_filename: str = "openstack-cluster-api-secret.yaml",
        cluster_filename: str = "openstack-cluster-api.yaml",
    ) -> None:
        secret_file = self.cluster_api_dir / secret_filename
        cluster_file = self.cluster_api_dir / cluster_filename

        for manifest in (secret_file, cluster_file):
            cmd = self._kubectl() + ["apply", "-f", str(manifest)]
            print(f"[ClusterAPI] Applying {manifest} ...")
            cp = subprocess.run(cmd, capture_output=True, text=True)
            if cp.returncode != 0:
                raise RuntimeError(f"[ClusterAPI] Failed to apply {manifest}:\n{cp.stderr}")
            print(cp.stdout.strip())

        print("[ClusterAPI] Manifests applied. Waiting for control plane to be ready...")

        start = time.time()
        while True:
            # A human-friendly describe helps while developing:
            desc = self._clusterctl() + ["describe", "cluster", cluster_name, "-n", namespace]
            cp = subprocess.run(desc, capture_output=True, text=True)
            out = cp.stdout if cp.returncode == 0 else cp.stderr
            print(out.strip())

            cluster_ready = f"Cluster/{cluster_name}" in out and "True" in out.split(f"Cluster/{cluster_name}")[1].split()[0]
            kcp = f"KubeadmControlPlane/{cluster_name}-control-plane"
            control_plane_ready = kcp in out and "True" in out.split(kcp)[1].split()[0]

            if cluster_ready and control_plane_ready:
                print("[ClusterAPI] Cluster and control plane are ready.")
                break

            if time.time() - start > timeout:
                raise TimeoutError(f"[ClusterAPI] Cluster {cluster_name} not ready after {timeout} seconds")

            time.sleep(interval)

        print("[ClusterAPI] Bootstrap completed successfully.")
