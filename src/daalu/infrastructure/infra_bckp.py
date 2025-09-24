import ansible_runner
import os
from pathlib import Path

# Adjust paths relative to repo root
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "playbooks"
PROJECT_DIR = BASE_DIR / "atmosphere" / "playbooks"   # where playbooks actually live


def deploy_infrastructure_b(inventory="inventory/hosts.ini", tags=None, extra_vars=None):
    """
    Deploy infrastructure using the infrastructure.yml playbook.
    - inventory: path to Ansible inventory file
    - tags: list of tags or sub-tags to run (e.g., ["metallb", "argocd"])
    - extra_vars: additional Ansible extra vars
    """
    playbook_name = "infrastructure.yml"

    # Ensure roles are discoverable
    env = os.environ.copy()
    env["ANSIBLE_ROLES_PATH"] = f"{BASE_DIR}/atmosphere/roles"

    rc = ansible_runner.run(
        private_data_dir=str(PROJECT_DIR),
        playbook=playbook_name,
        inventory=str(inventory),
        extravars=extra_vars or {},
        envvars=env,
        tags=",".join(tags) if tags else None   # ✅ Pass tags properly
    )

    if rc.rc != 0:
        raise RuntimeError(f"Infrastructure deployment failed: {rc.status}")
    return rc.status
