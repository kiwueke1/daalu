# src/daalu/hpc/scheduler/volcano.py

# src/daalu/hpc/scheduler/volcano.py

from daalu.helm.cli_runner import HelmCliRunner
from daalu.hpc.models import HPCConfig
import typer

class VolcanoDeployer:
    """
    Installs Volcano scheduler for batch AI/HPC workloads.
    """

    def __init__(self, kube_context: str):
        self.kube_context = kube_context
        self.helm = HelmCliRunner(kube_context=kube_context)

    def install(self, cfg: HPCConfig):
        typer.echo(f"[VolcanoDeployer] Installing Volcano scheduler in context '{self.kube_context}'")

        self.helm.add_repo("volcano", "https://volcano-sh.github.io/helm-charts")
        self.helm.update_repos()
        self.helm.install_release(
            name="volcano",
            chart="volcano/volcano",
            namespace="volcano-system",
            create_namespace=True,
        )

        typer.echo("[VolcanoDeployer] Volcano installed successfully.")
