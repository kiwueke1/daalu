# src/daalu/bootstrap/infrastructure/manager.py

from pathlib import Path

from daalu.utils.ssh_runner import SSHRunner
from daalu.bootstrap.infrastructure.engine.helm_engine import HelmInfraEngine
from daalu.bootstrap.infrastructure.engine.component import InfraComponent
from daalu.bootstrap.infrastructure.engine.infra_logging import InfraJsonlLogger, LoggedSSHRunner


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

        # ---- Create ONE infra run log file (structured JSONL) ----
        infra_logger = InfraJsonlLogger()
        infra_logger.log_event("infra.manager.start", components=[c.name for c in components])

        # ---- Wrap SSH so every command/transfer is captured ----
        # Host label is optional; if your SSHRunner knows the hostname, you can pass it in.
        logged_ssh = LoggedSSHRunner(self.ssh, infra_logger)

        # IMPORTANT: ensure HelmCliRunner uses the wrapped SSH too
        # (since helm commands run through helm.ssh.run)
        if getattr(self.helm, "ssh", None) is self.ssh:
            self.helm.ssh = logged_ssh

        # ---- Stage kubeconfig ONCE on controller ----
        kubeconfig_path = components[0].kubeconfig
        kubeconfig_text = Path(kubeconfig_path).read_text()

        infra_logger.set_stage("kubeconfig.stage")
        logged_ssh.put_text(
            kubeconfig_text,
            kubeconfig_path,
            sudo=True,
        )

        engine = HelmInfraEngine(
            helm=self.helm,
            ssh=logged_ssh,
            logger=infra_logger,  # new. for logging functionality.
        )

        for component in components:
            infra_logger.set_component(component.name)
            infra_logger.set_stage("component.deploy")
            infra_logger.log_event("infra.component.start", component=component.name)

            try:
                engine.deploy(component)
                infra_logger.log_event("infra.component.success", component=component.name)
            except Exception as e:
                infra_logger.log_event("infra.component.failed", component=component.name, error=str(e))
                raise

        infra_logger.set_stage("infra.complete")
        infra_logger.log_event("infra.manager.success")


    def pre_install(self, kubectl):
        """Run BEFORE Helm install/upgrade"""
        pass

    def post_install(self, kubectl):
        """Run AFTER Helm install/upgrade"""
        pass