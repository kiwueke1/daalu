import ansible_runner
import os
from pathlib import Path

# Repo root (daalu/daalu)
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent

# Ceph folder and playbooks
CEPH_DIR = BASE_DIR / "playbooks/ceph"
PLAYBOOKS_DIR = CEPH_DIR / "playbooks"
ROLES_DIR = CEPH_DIR / "roles"


def deploy_ceph(inventory="inventory/hosts.ini", tags=None, extra_vars=None):
    """
    Deploy Ceph using the site.yml playbook.
    - inventory: path to Ansible inventory file
    - tags: list of tags or sub-tags to run (e.g., ["mon", "osd"])
    - extra_vars: additional Ansible extra vars
    """
    playbook = PLAYBOOKS_DIR / "site.yml"

    # Ensure roles path is set to ceph/roles
    env = os.environ.copy()
    env["ANSIBLE_ROLES_PATH"] = str(ROLES_DIR)

    print(f"Running Ceph with roles from {env['ANSIBLE_ROLES_PATH']}")
    print(f"Using playbook: {playbook}")
    print(f"Using inventory: {inventory}")
    if tags:
        print(f"Running with tags: {','.join(tags)}")

    rc = ansible_runner.run(
        private_data_dir=str(BASE_DIR),
        playbook=str(playbook),
        inventory=str(inventory),
        extravars=extra_vars or {},
        envvars=env,
        tags=",".join(tags) if tags else None,   # ✅ Pass tags properly
    )

    if rc.rc != 0:
        raise RuntimeError(f"Ceph deployment failed: {rc.status}")
    return rc.status
