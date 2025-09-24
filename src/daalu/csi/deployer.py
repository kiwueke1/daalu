from pathlib import Path
from src.daalu.core.deployer import BaseDeployer


class CSIDeployer(BaseDeployer):
    def __init__(self, repo_root: Path):
        playbook_dir = repo_root / "playbooks" / "atmosphere" / "playbooks"
        roles_dir = repo_root / "playbooks" / "atmosphere" / "roles"
        super().__init__("CSI", repo_root, playbook_dir, roles_dir)

    def deploy(self, inventory: str, tags=None, extra_vars=None):
        return self.run("csi.yml", inventory, tags, extra_vars)
