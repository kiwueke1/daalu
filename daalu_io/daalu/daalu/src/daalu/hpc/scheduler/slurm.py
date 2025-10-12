# src/daalu/hpc/scheduler/slurm.py

from daalu.helm.cli_runner import HelmCliRunner
from daalu.hpc.models import HPCConfig
import typer

class SlurmDeployer:
    """
    Deploys Slurm on Kubernetes using Helm operator.
    """

    def __init__(self, kube_context: str):
        self.kube_context = kube_context
        self.helm = HelmCliRunner(kube_context=kube_context)

    def install(self, cfg: HPCConfig):
        typer.echo(f"[SlurmDeployer] Installing Slurm operator in context '{self.kube_context}'")

        self.helm.add_repo("stackhpc", "https://stackhpc.github.io/helm-charts/")
        self.helm.update_repos()
        self.helm.install_release(
            name="slurm-operator",
            chart="stackhpc/slurm-operator",
            namespace="slurm-system",
            create_namespace=True,
        )

        typer.echo("[SlurmDeployer] Slurm operator installed successfully.")
