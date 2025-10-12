# src/daalu/hpc/jobs.py

# src/daalu/hpc/jobs.py

import subprocess
import typer
from pathlib import Path
from daalu.hpc.models import HPCConfig

class JobClient:
    """
    Submits and monitors training jobs across supported schedulers.
    """

    def __init__(self, kube_context: str, scheduler: str):
        self.kube_context = kube_context
        self.scheduler = scheduler

    def submit(self, cfg: HPCConfig, spec: str):
        typer.echo(f"[JobClient] Submitting job spec '{spec}' using scheduler '{self.scheduler}'")
        try:
            subprocess.run(
                ["kubectl", "--context", self.kube_context, "apply", "-f", spec],
                check=True,
            )
            typer.echo("[JobClient] Job submitted successfully.")
        except subprocess.CalledProcessError as e:
            typer.echo(f"[JobClient] Job submission failed: {e}")
