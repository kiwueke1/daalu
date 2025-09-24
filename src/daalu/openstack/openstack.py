import ansible_runner
import os
from pathlib import Path

# Adjust paths relative to repo root
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "playbooks"
PROJECT_DIR = BASE_DIR / "atmosphere" / "playbooks"   # actual playbook dir


def deploy_openstack(inventory="inventory/hosts.ini", tags=None, extra_vars=None):
    """
    Deploy OpenStack using the openstack.yml playbook.
    - inventory: path to Ansible inventory file
    - tags: list of tags or sub-tags to run (e.g., ["keystone", "nova", "neutron"])
    - extra_vars: additional Ansible extra vars
    """
    playbook_name = "openstack.yml"  # ✅ just filename

    # Ensure roles are discoverable
    env = os.environ.copy()
    env["ANSIBLE_ROLES_PATH"] = f"{BASE_DIR}/atmosphere/roles"

    print(f"Running OpenStack from {env['ANSIBLE_ROLES_PATH']}")
    print(f"Using playbook: {PROJECT_DIR / playbook_name}")
    print(f"Using inventory: {inventory}")
    if tags:
        print(f"Running with tags: {','.join(tags)}")

    rc = ansible_runner.run(
        private_data_dir=str(PROJECT_DIR),       # ✅ point to playbook dir
        playbook=playbook_name,                  # ✅ just the filename
        inventory=str(inventory),
        extravars=extra_vars or {},
        envvars=env,
        tags=",".join(tags) if tags else None,   # ✅ tag filtering
    )

    if rc.rc != 0:
        raise RuntimeError(f"OpenStack deployment failed: {rc.status}")
    return rc.status
