from pathlib import Path
from src.daalu.core.deployer import BaseDeployer


class CephDeployer(BaseDeployer):
    def __init__(self, repo_root: Path):
        playbook_dir = repo_root / "playbooks" / "ceph" / "playbooks"
        roles_dir = repo_root / "playbooks" / "ceph" / "roles"
        super().__init__("Ceph", repo_root, playbook_dir, roles_dir)

    def deploy(self, inventory: str, tags=None, extra_vars=None):
        return self.run("site.yml", inventory, tags, extra_vars)
