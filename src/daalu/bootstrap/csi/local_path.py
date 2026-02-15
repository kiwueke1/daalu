# src/daalu/bootstrap/csi/local_path.py

from daalu.bootstrap.csi.base import CSIBase
from daalu.bootstrap.csi.helm_values import local_path_values
from daalu.bootstrap.csi.events import CSIStarted, CSISucceeded

class LocalPathCsiDriver(CSIBase):
    def __init__(self, bus, helm):
        super().__init__(bus)
        self.helm = helm

    def deploy(self, cfg):
        self.bus.emit(CSIStarted(
            stage="init",
            message="Deploying local-path-provisioner",
            **self._ctx(),
        ))

        self.helm.install_or_upgrade(
            name="local-path-provisioner",
            chart="charts/local-path-provisioner",
            namespace="local-path-storage",
            values=local_path_values(),
            kubeconfig=cfg.kubeconfig_path,
        )

        self.bus.emit(CSISucceeded(
            stage="completed",
            message="Local Path Provisioner deployed",
            **self._ctx(),
        ))
