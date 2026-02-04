# src/daalu/bootstrap/monitoring/manager.py

from daalu.bootstrap.engine.helm_engine import HelmInfraEngine
from daalu.bootstrap.engine.infra_logging import InfraJsonlLogger
from daalu.helm.cli_runner import HelmCliRunner
from daalu.utils.ssh_runner import SSHRunner


class MonitoringManager:
    def __init__(self, *, helm: HelmCliRunner, ssh: SSHRunner):
        self.helm = helm
        self.ssh = ssh

    def deploy(self, components):
        logger = InfraJsonlLogger()
        engine = HelmInfraEngine(
            helm=self.helm,
            ssh=self.ssh,
            logger=logger,
        )

        for component in components:
            engine.deploy(component)
