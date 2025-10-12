# src/daalu/hpc/cli.py
import typer
from .provisioner import HPCProvisioner
from .runtime import RuntimeDeployer
from .scheduler.volcano import VolcanoDeployer
from .scheduler.ray import RayDeployer
from .scheduler.slurm import SlurmDeployer
from .jobs import JobClient
from .models import HPCConfig

app = typer.Typer(help="AI/HPC cluster lifecycle")

@app.command("bootstrap")
def bootstrap(config: str, mgmt_context: str = typer.Option(..., "--mgmt-context")):
    cfg = HPCConfig.from_file(config)
    HPCProvisioner(mgmt_context).create_cluster(cfg)

@app.command("enable-runtime")
def enable_runtime(config: str, context: str):
    cfg = HPCConfig.from_file(config)
    RuntimeDeployer(context).install_gpu_stack(cfg)

@app.command("enable-scheduler")
def enable_scheduler(config: str, context: str, kind: str = typer.Option("volcano")):
    cfg = HPCConfig.from_file(config)
    if kind == "volcano":
        VolcanoDeployer(context).install(cfg)
    elif kind == "ray":
        RayDeployer(context).install(cfg)
    elif kind == "slurm":
        SlurmDeployer(context).install(cfg)
    else:
        raise typer.BadParameter(f"Unknown scheduler: {kind}")

@app.command("enable-storage")
def enable_storage(config: str, context: str):
    from .storage import StorageDeployer
    cfg = HPCConfig.from_file(config)
    StorageDeployer(context).install(cfg)

@app.command("submit")
def submit(config: str, context: str, spec: str, scheduler: str = "volcano"):
    cfg = HPCConfig.from_file(config)
    JobClient(context, scheduler).submit(cfg, spec)

cli = app
