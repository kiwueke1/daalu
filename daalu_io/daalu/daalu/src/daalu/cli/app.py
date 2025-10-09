import os
from pathlib import Path
import typer

from daalu.config.loader import load_config
from daalu.helm.cli_runner import HelmCliRunner
from daalu.deploy.executor import deploy_all, DeployOptions
from typing import Optional


# -------------------------------------------------------------------
# Set workspace root for Bazel hermetic paths
# -------------------------------------------------------------------
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("WORKSPACE_ROOT", str(WORKSPACE_ROOT))

print(WORKSPACE_ROOT)
print("workspace root above")

# -------------------------------------------------------------------
# Typer CLI definition
# -------------------------------------------------------------------
app = typer.Typer(help="Daalu Deployment CLI")


@app.command()
def deploy(
    config: str,
    context: Optional[str] = typer.Argument(None, help="Kubernetes context name"),
    debug: bool = typer.Option(False, "--debug", "-d", help="Enable Helm debug output (streams helm logs live)")
):
    """
    Deploy all OpenStack/K8s components using Helm.
    
    Example:
        bazel run //src/daalu:cli_app -- deploy cluster-defs/cluster.yaml my-workload-ctx --debug
    """

    debug=True
    print("debug option is:", debug)
    print("context is:", context)


    typer.echo(f"Loading config from {config}")
    cfg = load_config(config)

    # Instantiate Helm runner for the selected kube-context
    helm = HelmCliRunner(context)

    # Pass debug flag into deployment options
    options = DeployOptions(debug=debug)

    typer.echo(f"Starting deployment in context: {context}")
    if debug:
        typer.echo("Debug mode enabled: Helm output will be streamed.\n")

    # Execute the deployment
    report = deploy_all(cfg, helm, options=options)

    typer.echo("\nDeployment summary:")
    typer.echo(report.summary())


@app.command()
def cleanup(
    context: str = typer.Option(None, "--context", "-c", help="Kubernetes context to clean up"),
    debug: bool = typer.Option(False, "--debug", "-d", help="Enable Helm debug output (streams helm logs live)")
):
    """Clean up cluster resources."""
    typer.echo("Cleaning up cluster resources...")

    helm = HelmCliRunner(context)
    # Placeholder for future cleanup logic
    # Example: helm.uninstall("keystone", "openstack", debug=debug)
    typer.echo("Cleanup completed.")


# -------------------------------------------------------------------
# Entrypoint
# -------------------------------------------------------------------
if __name__ == "__main__":
    app()
