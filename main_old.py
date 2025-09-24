from pathlib import Path
from datetime import datetime
import sys
import argparse

from src.daalu import setup, ceph, kubernetes, csi, infrastructure, monitoring, openstack
from src.daalu.bootstrap import cluster_api


# Path to cloud-config
CLOUD_CONFIG = Path(__file__).resolve().parent / "cloud-config"

# Path to logs directory
LOGS_DIR = Path(__file__).resolve().parent / "logs"
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

    # Map top-level tags to their deployment functions
    steps = {
        "cluster_api": lambda t=None: cluster_api.deploy_cluster_api(),
        "setup": lambda t=None: setup.deploy_setup(tags=t),
        "ceph": lambda t=None: ceph.deploy_ceph(inventory, tags=t),
        "kubernetes": lambda t=None: kubernetes.deploy_kubernetes(inventory, tags=t),
        "csi": lambda t=None: csi.deploy_csi(inventory, tags=t),
        "infrastructure": lambda t=None: infrastructure.deploy_infrastructure_b(inventory, tags=t),
        "monitoring": lambda t=None: monitoring.deploy_monitoring(inventory, tags=t),
        "openstack": lambda t=None: openstack.deploy_openstack(inventory, tags=t),
    }

    # Known sub-tags → their parent playbooks
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
        # add more mappings here as needed
    }

    # If no tags specified, run everything
    if not selected_tags:
        selected_tags = list(steps.keys())

    for tag in selected_tags:
        if tag in steps:
            print(f"Running step: {tag}")
            steps[tag]()  # full playbook
            print(f"Completed: {tag}")

        elif tag in sub_tag_map:
            parent = sub_tag_map[tag]
            print(f"Running sub-tag '{tag}' inside '{parent}' playbook...")
            steps[parent](t=[tag])
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
