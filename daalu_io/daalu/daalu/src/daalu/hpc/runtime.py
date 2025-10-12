# src/daalu/hpc/runtime.py

# src/daalu/hpc/runtime.py

from daalu.helm.cli_runner import HelmCliRunner
from daalu.hpc.models import HPCConfig
import typer

class RuntimeDeployer:
    """
    Installs GPU runtime components (Node Feature Discovery,
    NVIDIA GPU Operator, and DCGM exporter).
    """

    def __init__(self, kube_context: str):
        self.kube_context = kube_context
        self.helm = HelmCliRunner(kube_context=kube_context)

    def install_gpu_stack(self, cfg: HPCConfig):
        typer.echo(f"[RuntimeDeployer] Installing GPU stack in context '{self.kube_context}'")

        # Placeholder: add Helm repos and install charts
        self.helm.add_repo(name="nvidia", url="https://nvidia.github.io/gpu-operator")
        self.helm.update_repos()

        self.helm.install_release(
            name="node-feature-discovery",
            chart="nvidia/node-feature-discovery",
            namespace="gpu-operator",
            create_namespace=True,
        )

        self.helm.install_release(
            name="gpu-operator",
            chart="nvidia/gpu-operator",
            namespace="gpu-operator",
            create_namespace=True,
        )

        # DCGM exporter (GPU metrics to Prometheus)
        self.helm.add_repo(name="prometheus-community", url="https://prometheus-community.github.io/helm-charts")
        self.helm.install_release(
            name="dcgm-exporter",
            chart="prometheus-community/dcgm-exporter",
            namespace="monitoring",
            create_namespace=True,
        )

        typer.echo("[RuntimeDeployer] GPU stack installation complete.")
