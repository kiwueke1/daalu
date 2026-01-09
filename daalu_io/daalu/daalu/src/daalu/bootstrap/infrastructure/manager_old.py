# src/daalu/bootstrap/infrastructure/manager.py

from pathlib import Path

from daalu.utils.ssh_runner import SSHRunner
from daalu.bootstrap.infrastructure.engine.helm_engine import HelmInfraEngine
from daalu.bootstrap.infrastructure.engine.component import InfraComponent


class InfrastructureManager:
    """
    Orchestrates infrastructure component deployment on the controller node.

    IMPORTANT:
    - Does NOT create or close SSH connections.
    - SSH lifecycle is owned by the caller (cli/app.py).
    """

    def __init__(self, *, helm, ssh: SSHRunner):
        self.helm = helm
        self.ssh = ssh

    def deploy(self, components: list[InfraComponent]) -> None:
        if not components:
            return

        # ---- Stage kubeconfig ONCE on controller ----
        kubeconfig_path = components[0].kubeconfig
        kubeconfig_text = Path(kubeconfig_path).read_text()

        self.ssh.put_text(
            kubeconfig_text,
            kubeconfig_path,
            sudo=True,
        )

        engine = HelmInfraEngine(
            helm=self.helm,
            ssh=self.ssh,
        )

        for component in components:
            engine.deploy(component)
