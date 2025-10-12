# src/daalu/hpc/storage.py

# src/daalu/hpc/storage.py

from daalu.helm.cli_runner import HelmCliRunner
from daalu.hpc.models import HPCConfig
import typer

class StorageDeployer:
    """
    Deploys storage components: Ceph CSI, MinIO, and local NVMe PV provisioner.
    """

    def __init__(self, kube_context: str):
        self.kube_context = kube_context
        self.helm = HelmCliRunner(kube_context=kube_context)

    def install(self, cfg: HPCConfig):
        typer.echo(f"[StorageDeployer] Setting up storage in context '{self.kube_context}'")

        if cfg.storage.ceph:
            self.helm.add_repo("ceph", "https://ceph.github.io/csi-charts")
            self.helm.install_release(
                name="ceph-csi",
                chart="ceph/ceph-csi",
                namespace="ceph-csi",
                create_namespace=True,
            )

        if cfg.storage.minio:
            self.helm.add_repo("minio", "https://charts.min.io/")
            self.helm.install_release(
                name="minio",
                chart="minio/minio",
                namespace="storage",
                create_namespace=True,
                values={"mode": "standalone"},
            )

        typer.echo("[StorageDeployer] Storage components installed.")
