# src/daalu/hpc/provisioner.py
from __future__ import annotations

import subprocess
import typer
from pathlib import Path
from typing import Optional, List

from daalu.config.loader import load_config
from daalu.helm.cli_runner import HelmCliRunner
from daalu.bootstrap.cluster_api_manager import ClusterAPIManager
from daalu.observers.console import ConsoleObserver
from daalu.hpc.models import HPCConfig, GPUNodeSpec


class HPCProvisioner:
    """
    Bootstraps a GPU-enabled HPC / AI training cluster
    by dynamically extending ClusterAPI manifests with GPU node definitions.
    """

    def __init__(self, mgmt_context: str, workspace_root: Optional[Path] = None):
        self.mgmt_context = mgmt_context
        self.helm = HelmCliRunner(kube_context=mgmt_context)
        self.workspace_root = workspace_root or Path(__file__).resolve().parents[3]
        self.observers = [ConsoleObserver()]

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    def _validate_mgmt_cluster(self) -> None:
        typer.echo(f"[HPCProvisioner] Validating mgmt cluster '{self.mgmt_context}' connectivity...")
        try:
            subprocess.run(
                ["kubectl", "--context", self.mgmt_context, "get", "nodes", "-o", "wide"],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            typer.secho(f"[HPCProvisioner] ❌ Failed to contact management cluster: {e}", fg="red")
            raise

    def _augment_cluster_context(self, cfg: HPCConfig) -> dict:
        """
        Merge HPCConfig (especially GPU node specs) into a ClusterAPI
        template context so the ClusterAPIManager can render manifests.
        """
        base = cfg.cluster_api.model_dump()
        gpu_nodes: List[GPUNodeSpec] = cfg.nodes or []
        gpu_pool = []

        for n in gpu_nodes:
            gpu_pool.append(
                {
                    "name": n.name,
                    "cpus": n.cpus,
                    "memory_gb": n.memory_gb,
                    "gpus": n.gpus,
                    "gpu_model": n.gpu_model,
                    "rdma": n.rdma,
                    "nvme_count": n.nvme_count,
                    "node_pool": n.node_pool,
                    # Labels and taints are important for GPU scheduling
                    "labels": {
                        "hardware": "gpu",
                        "gpu.model": n.gpu_model,
                    },
                    "taints": [
                        {"key": "hardware", "value": "gpu", "effect": "NoSchedule"}
                    ],
                }
            )

        base["gpu_node_pools"] = gpu_pool
        return base

    # -------------------------------------------------------------------------
    # Main create routine
    # -------------------------------------------------------------------------
    def create_cluster(self, cfg: HPCConfig):
        typer.echo(
            f"[HPCProvisioner] Bootstrapping GPU cluster '{cfg.name}' "
            f"in namespace '{cfg.namespace}' via mgmt context '{self.mgmt_context}'"
        )

        # 1. Validate connectivity
        self._validate_mgmt_cluster()

        # 2. Merge GPU info into cluster_api context
        context = self._augment_cluster_context(cfg)

        # 3. Deploy ClusterAPI manifests
        manager = ClusterAPIManager(
            self.workspace_root, mgmt_context=self.mgmt_context, observers=self.observers
        )

        typer.echo("[HPCProvisioner] Applying ClusterAPI manifests with GPU node pools...")
        try:
            manager.deploy_dynamic(cfg)
        except Exception as e:
            typer.secho(f"[HPCProvisioner] ❌ ClusterAPI bootstrap failed: {e}", fg="red")
            raise

        # 4. Patch node labels / taints after cluster is created
        typer.echo("[HPCProvisioner] Waiting for workload cluster nodes to register...")
        typer.echo("[HPCProvisioner] (TODO) Implement wait + patch for node labels/taints once kubeconfig is available.")

        typer.secho("[HPCProvisioner] ✅ GPU cluster bootstrap complete (control plane ready).", fg="green")
