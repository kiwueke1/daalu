import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent/"playbooks"
CLUSTER_API_DIR = BASE_DIR / "cluster-api"

def deploy_cluster_api(cluster_name="openstack-infra", namespace="default", timeout=1800, interval=180):
    """
    Deploy Kubernetes cluster using Cluster API + Proxmox.
    Waits until the control plane is ready before continuing.
    """
    secret_file = CLUSTER_API_DIR / "openstack-cluster-api-secret.yaml"
    cluster_file = CLUSTER_API_DIR / "openstack-cluster-api.yaml"

    for manifest in [secret_file, cluster_file]:
        cmd = ["kubectl", "apply", "-f", str(manifest)]
        print(f"Applying {manifest} ...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to apply {manifest}:\n{result.stderr}")
        else:
            print(result.stdout.strip())

    print("Cluster API manifests applied successfully. Waiting for control plane to become ready...")

    # Wait for control plane and cluster readiness
    start = time.time()
    while True:
        cmd = ["clusterctl", "describe", "cluster", cluster_name, "-n", namespace]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr)
        else:
            output = result.stdout
            print(output)

            # Conditions to check
            cluster_ready = "Cluster/" + cluster_name in output and "True" in output.split("Cluster/" + cluster_name)[1].split()[0]
            control_plane_ready = "KubeadmControlPlane/" + cluster_name + "-control-plane" in output and "True" in output.split("KubeadmControlPlane/" + cluster_name + "-control-plane")[1].split()[0]

            if cluster_ready and control_plane_ready:
                print("Cluster and control plane are ready.")
                break

        if time.time() - start > timeout:
            raise TimeoutError(f"Cluster {cluster_name} not ready after {timeout} seconds")

        print(f"Waiting {interval}s before checking again...")
        time.sleep(interval)

    print("Cluster API bootstrap completed successfully.")
