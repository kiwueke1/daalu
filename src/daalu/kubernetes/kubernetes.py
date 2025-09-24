import ansible_runner
import os
from pathlib import Path

# Adjust paths relative to repo root
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "playbooks"
ANSIBLE_DIR = BASE_DIR / "atmosphere" / "playbooks"


def deploy_kubernetes(inventory="inventory/hosts.ini", tags=None, extra_vars=None):
    """
    Deploy Kubernetes using the kubernetes.yml playbook.
    - inventory: path to Ansible inventory file
    - tags: list of tags or sub-tags to run (e.g., ["calico", "cilium"])
    - extra_vars: additional Ansible extra vars
    """
    playbook = ANSIBLE_DIR / "kubernetes.yml"

    # Ensure roles are discoverable
    env = os.environ.copy()
    env["ANSIBLE_ROLES_PATH"] = f"{BASE_DIR}/atmosphere/roles"

    print(f"Running Kubernetes from {env['ANSIBLE_ROLES_PATH']}")
    print(f"Using playbook: {playbook}")
    print(f"Using inventory: {inventory}")
    if tags:
        print(f"Running with tags: {','.join(tags)}")

    rc = ansible_runner.run(
        private_data_dir=str(ANSIBLE_DIR),   # set project dir where playbooks live
        playbook="kubernetes.yml",           # just filename, not abs path
        inventory=str(inventory),
        extravars=extra_vars or {},
        envvars=env,
        tags=",".join(tags) if tags else None,
    )

    if rc.rc != 0:
        raise RuntimeError(f"Kubernetes deployment failed: {rc.status}")
    return rc.status
