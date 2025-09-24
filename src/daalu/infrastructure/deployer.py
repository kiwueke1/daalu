from pathlib import Path
from src.daalu.core.deployer import BaseDeployer


class InfrastructureDeployer(BaseDeployer):
    def __init__(self, repo_root: Path):
        playbook_dir = repo_root / "playbooks" / "atmosphere" / "playbooks"
        roles_dir = repo_root / "playbooks" / "atmosphere" / "roles"
        super().__init__("Infrastructure", repo_root, playbook_dir, roles_dir)

    def deploy(self, inventory: str, tags=None, extra_vars=None):
        return self.run("infrastructure.yml", inventory, tags, extra_vars)
    # After infra, run deployKF sync script
        script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "sync_argocd_apps.sh"
        try:
            subprocess.run([str(script_path)], check=True)
            print("✅ ArgoCD apps synced successfully.")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"ArgoCD sync failed: {e}")
