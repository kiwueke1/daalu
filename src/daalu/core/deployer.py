# src/daalu/core/deployer.py
import os
from pathlib import Path
import ansible_runner


class BaseDeployer:
    def __init__(self, name: str, base_dir: Path, playbook_dir: Path, roles_dir: Path):
        self.name = name
        self.base_dir = base_dir
        self.playbook_dir = playbook_dir
        self.roles_dir = roles_dir

    def run(self, playbook: str, inventory: str, tags=None, extra_vars=None):
        env = os.environ.copy()
        env["ANSIBLE_ROLES_PATH"] = str(self.roles_dir)

        print(f"[{self.name}] Running playbook: {playbook}")
        print(f"[{self.name}] Inventory: {inventory}")
        if tags:
            print(f"[{self.name}] Tags: {','.join(tags)}")

        rc = ansible_runner.run(
            private_data_dir=str(self.playbook_dir),
            playbook=playbook,
            inventory=inventory,
            extravars=extra_vars or {},
            envvars=env,
            tags=",".join(tags) if tags else None,
        )

        if rc.rc != 0:
            raise RuntimeError(f"{self.name} deployment failed: {rc.status}")
        return rc.status
