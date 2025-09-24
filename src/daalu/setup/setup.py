import ansible_runner
import os
from pathlib import Path

# Adjust paths relative to repo root
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "playbooks"
PROJECT_DIR = BASE_DIR / "setup" / "playbooks"   # where playbooks actually live


def deploy_setup(inventory=PROJECT_DIR/"inventory/hosts.ini", tags=None, extra_vars=None):
    """
    Deploy setup using the setup.yaml playbook.
    - inventory: path to Ansible inventory file
    - tags: list of tags or sub-tags to run (e.g., ["init", "prereqs"])
    - extra_vars: additional Ansible extra vars
    """
    playbook_name = "setup.yaml"

    # Ensure roles are discoverable
    env = os.environ.copy()
    env["ANSIBLE_ROLES_PATH"] = f"{BASE_DIR}/setup/roles"

    rc = ansible_runner.run(
        private_data_dir=str(PROJECT_DIR),
        playbook=playbook_name,
        inventory=str(inventory),
        extravars=extra_vars or {},
        envvars=env,
        tags=",".join(tags) if tags else None   # ✅ Pass tags properly
    )

    if rc.rc != 0:
        raise RuntimeError(f"Setup playbook failed: {rc.status}")
    return rc.status
