# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/monitoring/manager.py

from daalu.bootstrap.engine.helm_engine import HelmInfraEngine
from daalu.bootstrap.engine.infra_logging import InfraJsonlLogger
from daalu.helm.cli_runner import HelmCliRunner
from daalu.utils.ssh_runner import SSHRunner


class MonitoringManager:
    def __init__(
        self,
        *,
        helm: HelmCliRunner,
        ssh: SSHRunner,
        registry_url: str | None = None,
        registry_project: str = "openstack",
    ):
        self.helm = helm
        self.ssh = ssh
        self.registry_url = registry_url
        self.registry_project = registry_project

    def deploy(self, components):
        logger = InfraJsonlLogger()
        engine = HelmInfraEngine(
            helm=self.helm,
            ssh=self.ssh,
            logger=logger,
            registry_url=self.registry_url,
            registry_project=self.registry_project,
        )

        for component in components:
            engine.deploy(component)
