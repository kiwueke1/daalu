import subprocess
import time
from pathlib import Path


class ClusterAPIManager:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.base_dir = repo_root / "playbooks"
        self.cluster_api_dir = self.base_dir / "cluster-api"

    def deploy(self, inventory=None, cluster_name="openstack-infra", namespace="default", timeout=1800, interval=180):
        """
        Deploy Kubernetes cluster using Cluster API + Proxmox.
        Waits until the control plane is ready before continuing.
        """
        secret_file = self.cluster_api_dir / "openstack-cluster-api-secret.yaml"
        cluster_file = self.cluster_api_dir / "openstack-cluster-api.yaml"

        # Apply manifests
        for manifest in [secret_file, cluster_file]:
            cmd = ["kubectl", "apply", "-f", str(manifest)]
            print(f"[ClusterAPI] Applying {manifest} ...")
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"[ClusterAPI] Failed to apply {manifest}:\n{result.stderr}")
            else:
                print(result.stdout.strip())

        print("[ClusterAPI] Manifests applied successfully. Waiting for control plane to become ready...")

        # Wait for readiness
        start = time.time()
        while True:
            cmd = ["clusterctl", "describe", "cluster", cluster_name, "-n", namespace]
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                print(result.stderr)
            else:
                output = result.stdout
                print(output)

                cluster_ready = (
                    f"Cluster/{cluster_name}" in output
                    and "True" in output.split(f"Cluster/{cluster_name}")[1].split()[0]
                )
                control_plane_ready = (
                    f"KubeadmControlPlane/{cluster_name}-control-plane" in output
                    and "True" in output.split(f"KubeadmControlPlane/{cluster_name}-control-plane")[1].split()[0]
                )

                if cluster_ready and control_plane_ready:
                    print("[ClusterAPI] Cluster and control plane are ready.")
                    break

            if time.time() - start > timeout:
                raise TimeoutError(f"[ClusterAPI] Cluster {cluster_name} not ready after {timeout} seconds")

            print(f"[ClusterAPI] Waiting {interval}s before checking again...")
            time.sleep(interval)

        print("[ClusterAPI] Bootstrap completed successfully.")
