from pathlib import Path
from src.daalu.core.deployer import BaseDeployer


class SetupDeployer(BaseDeployer):
    def __init__(self, repo_root: Path):
        playbook_dir = repo_root / "playbooks" / "setup" / "playbooks"
        roles_dir = repo_root / "playbooks" / "setup" / "roles"
        super().__init__("Setup", repo_root, playbook_dir, roles_dir)

    def deploy(self, inventory=None, tags=None, extra_vars=None):
        return self.run("setup.yaml", inventory, tags, extra_vars)
