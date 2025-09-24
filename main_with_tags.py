from pathlib import Path
from datetime import datetime
import sys
import argparse

from src.daalu.kubernetes.deployer import KubernetesDeployer
from src.daalu.ceph.deployer import CephDeployer
from src.daalu.openstack.deployer import OpenStackDeployer
from src.daalu.setup.deployer import SetupDeployer
from src.daalu.infrastructure.deployer import InfrastructureDeployer
from src.daalu.monitoring.deployer import MonitoringDeployer
from src.daalu.csi.deployer import CSIDeployer
from src.daalu.bootstrap.cluster_api_deployer import ClusterAPIManager  # OOP refactor of your cluster_api


# Path to repo root
REPO_ROOT = Path(__file__).resolve().parent

# Path to cloud-config inventory
CLOUD_CONFIG = REPO_ROOT / "cloud-config"

# Path to logs directory
LOGS_DIR = REPO_ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def create_log_file(tags):
    """Create a log file with timestamp and tags in its name."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    if tags:
        tag_str = "_".join(tags)
        filename = f"daalu_logs_{timestamp}_{tag_str}.log"
    else:
        filename = f"daalu_logs_{timestamp}.log"
    return LOGS_DIR / filename


def deploy_all(selected_tags=None):
    inventory = CLOUD_CONFIG / "inventory" / "hosts.ini"

    # Map top-level tags to their deployer classes
    steps = {
        "cluster_api": ClusterAPIManager(REPO_ROOT),
        "setup": SetupDeployer(REPO_ROOT),
        "ceph": CephDeployer(REPO_ROOT),
        "kubernetes": KubernetesDeployer(REPO_ROOT),
        "csi": CSIDeployer(REPO_ROOT),
        "infrastructure": InfrastructureDeployer(REPO_ROOT),
        "monitoring": MonitoringDeployer(REPO_ROOT),
        "openstack": OpenStackDeployer(REPO_ROOT),
    }

    # Known sub-tags → parent component
    sub_tag_map = {
        # infrastructure sub-tags
        "metallb": "infrastructure",
        "argocd": "infrastructure",
        "istio": "infrastructure",
        "keycloak": "infrastructure",
        "cert-manager": "infrastructure",
        "kubeflow": "infrastructure",
        "ingress-nginx": "infrastructure",
        # kubernetes sub-tags
        "calico": "kubernetes",
        "cilium": "kubernetes",
        # add more mappings as needed
        "barbican": "openstack"
    }

    # If no tags specified, run everything
    if not selected_tags:
        selected_tags = list(steps.keys())

    for tag in selected_tags:
        if tag in steps:
            print(f"Running step: {tag}")
            steps[tag].deploy(str(inventory))
            print(f"Completed: {tag}")

        elif tag in sub_tag_map:
            parent = sub_tag_map[tag]
            print(f"Running sub-tag '{tag}' inside '{parent}' playbook...")
            steps[parent].deploy(str(inventory), tags=[tag])
            print(f"Completed sub-tag: {tag}")

        else:
            print(f"Unknown tag: {tag}")

    print("Deployment finished!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daalu deployment runner")
    parser.add_argument(
        "--tags",
        help="Comma-separated list of components or sub-tags to deploy "
             "(e.g. --tags kubernetes,csi,metallb,argocd)",
        default=""
    )
    args = parser.parse_args()

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    # Create log file
    log_file = create_log_file(tags)
    print(f"saving logs to: {log_file.resolve()}")

    # Redirect stdout and stderr to the log file
    sys.stdout = open(log_file, "w")
    sys.stderr = sys.stdout

    # Run deployments
    deploy_all(tags)

    # Flush logs
    sys.stdout.flush()
    sys.stderr.flush()

    # Restore stdout for final confirmation
    sys.stdout = sys.__stdout__
    print(f"Logs saved to: {log_file.resolve()}")
