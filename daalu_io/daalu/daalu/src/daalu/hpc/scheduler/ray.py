# src/daalu/hpc/scheduler/ray.py

from daalu.helm.cli_runner import HelmCliRunner
from daalu.hpc.models import HPCConfig
import typer

class RayDeployer:
    """
    Deploys the Ray operator for distributed Python training.
    """

    def __init__(self, kube_context: str):
        self.kube_context = kube_context
        self.helm = HelmCliRunner(kube_context=kube_context)

    def install(self, cfg: HPCConfig):
        typer.echo(f"[RayDeployer] Installing Ray operator in context '{self.kube_context}'")

        self.helm.add_repo("kuberay", "https://ray-project.github.io/kuberay-helm/")
        self.helm.update_repos()
        self.helm.install_release(
            name="kuberay-operator",
            chart="kuberay/kuberay-operator",
            namespace="ray-system",
            create_namespace=True,
        )

        typer.echo("[RayDeployer] Ray operator installed successfully.")
