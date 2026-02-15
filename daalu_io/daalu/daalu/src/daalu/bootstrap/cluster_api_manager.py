from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, List, Any

from daalu.execution.runner import CommandRunner
from .template_renderer import TemplateRenderer

# Observer system imports
from ..observers.dispatcher import EventBus
from ..observers.events import (
    new_ctx,
    ClusterAPIStarted,
    ManifestApplied,
    ClusterAPIStatusUpdate,
    ClusterAPIReady,
    ClusterAPITimedOut,
    ClusterAPIFailed,
    ClusterAPISummary,
)
from ..config.models import ClusterConfig
import logging

log = logging.getLogger("daalu")


class ClusterAPIManager:
    """
    Applies Cluster API manifests on the MANAGEMENT cluster and waits until the
    workload control-plane is ready, while emitting observer events for progress tracking.
    """

    def __init__(
        self,
        repo_root: Path,
        mgmt_context: Optional[str] = None,
        kubeconfig: Optional[str] = None,
        observers: Optional[List] = None,
        ctx: Any = None,
    ):
        self.repo_root = repo_root
        self.mgmt_context = mgmt_context
        self.kubeconfig = kubeconfig
        self.base_dir = repo_root / "cluster-defs"
        self.cluster_api_dir = self.base_dir / "cluster-api"

        self.ctx = ctx
        self.runner = CommandRunner(
            logger=getattr(ctx, "logger", None),
            dry_run=getattr(ctx, "dry_run", False),
        )

        # EventBus setup
        self.bus = EventBus(observers or [])
        self.run_ctx = new_ctx(env="mgmt", context=mgmt_context or "default")

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _kubectl(self) -> list[str]:
        cmd = ["kubectl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        if self.mgmt_context:
            cmd += ["--context", self.mgmt_context]
        return cmd

    def _clusterctl(self) -> list[str]:
        cmd = ["clusterctl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        return cmd

    # -------------------------------------------------------------------------
    # Render manifests only
    # -------------------------------------------------------------------------

    def render_dynamic(self, config: ClusterConfig) -> str:
        """
        Render the full Cluster API manifests (Secret + Cluster YAML) into
        a single combined YAML string without applying them.
        """
        templates_dir = self.repo_root / "templates/cluster-api"
        renderer = TemplateRenderer(templates_dir)
        context = config.cluster_api.model_dump()

        rendered_docs = []
        for tmpl in ["cluster-api-secret.yaml.j2", "cluster-api.yaml.j2"]:
            manifest = renderer.render(tmpl, context)
            rendered_docs.append(manifest.strip())

        return "\n---\n".join(rendered_docs) + "\n"

    # -------------------------------------------------------------------------
    # Dynamic deploy (render + apply)
    # -------------------------------------------------------------------------

    def deploy_dynamic(self, config: ClusterConfig) -> None:
        self.bus.emit(
            ClusterAPIStarted(
                name=config.cluster_api.cluster_name,
                namespace=config.cluster_api.namespace,
                **self.run_ctx,
            )
        )

        templates_dir = self.repo_root / "templates/cluster-api"
        renderer = TemplateRenderer(templates_dir)
        context = config.cluster_api.model_dump()

        for tmpl in ["cluster-api-secret.yaml.j2", "cluster-api.yaml.j2"]:
            log.debug(f"[ClusterAPI] Applying {tmpl} ...")
            manifest = renderer.render(tmpl, context)

            result = self.runner.run(
                self._kubectl() + ["apply", "-f", "-"],
                stdin_text=manifest,
                capture_output=True,
                check=False,
            )

            if result.returncode != 0:
                self.bus.emit(
                    ClusterAPIFailed(
                        name=tmpl,
                        error=(result.stderr or "").strip(),
                        **self.run_ctx,
                    )
                )
                raise RuntimeError(
                    f"Failed to apply {tmpl}: {result.stderr}"
                )

            self.bus.emit(
                ManifestApplied(
                    name=tmpl,
                    output=(result.stdout or "").strip(),
                    **self.run_ctx,
                )
            )

    # -------------------------------------------------------------------------
    # Static deploy (pre-rendered files)
    # -------------------------------------------------------------------------

    def deploy(
        self,
        cluster_name: str = "openstack-infra",
        namespace: str = "default",
        timeout: int = 1800,
        interval: int = 30,
        secret_filename: str = "openstack-cluster-api-secret.yaml",
        cluster_filename: str = "openstack-cluster-api.yaml",
    ) -> None:
        self.bus.emit(
            ClusterAPIStarted(
                name=cluster_name,
                namespace=namespace,
                **self.run_ctx,
            )
        )

        secret_file = self.cluster_api_dir / secret_filename
        cluster_file = self.cluster_api_dir / cluster_filename

        try:
            # -------------------------------------------------------------
            # Apply manifests
            # -------------------------------------------------------------
            for manifest in (secret_file, cluster_file):
                cmd = self._kubectl() + ["apply", "-f", str(manifest)]
                log.debug(f"[ClusterAPI] Applying {manifest} ...")

                result = self.runner.run(
                    cmd,
                    capture_output=True,
                    check=False,
                )

                if result.returncode != 0:
                    self.bus.emit(
                        ClusterAPIFailed(
                            name=cluster_name,
                            error=(result.stderr or "").strip(),
                            **self.run_ctx,
                        )
                    )
                    raise RuntimeError(
                        f"[ClusterAPI] Failed to apply {manifest}:\n{result.stderr}"
                    )

                self.bus.emit(
                    ManifestApplied(
                        name=manifest.name,
                        output=(result.stdout or "").strip(),
                        **self.run_ctx,
                    )
                )

            log.debug(
                "[ClusterAPI] Manifests applied. Waiting for control plane to be ready..."
            )

            # -------------------------------------------------------------
            # Poll readiness via clusterctl
            # -------------------------------------------------------------
            start = time.time()

            while True:
                desc_cmd = self._clusterctl() + [
                    "describe",
                    "cluster",
                    cluster_name,
                    "-n",
                    namespace,
                ]

                result = self.runner.run(
                    desc_cmd,
                    capture_output=True,
                    check=False,
                )

                out = (
                    result.stdout
                    if result.returncode == 0
                    else result.stderr
                ) or ""

                self.bus.emit(
                    ClusterAPIStatusUpdate(
                        name=cluster_name,
                        output=out.strip(),
                        **self.run_ctx,
                    )
                )

                # Same crude readiness heuristic (unchanged)
                cluster_ready = (
                    f"Cluster/{cluster_name}" in out
                    and "True"
                    in out.split(f"Cluster/{cluster_name}")[1].split()[0]
                )

                kcp = f"KubeadmControlPlane/{cluster_name}-control-plane"
                control_plane_ready = (
                    kcp in out
                    and "True"
                    in out.split(kcp)[1].split()[0]
                )

                if cluster_ready and control_plane_ready:
                    log.debug("[ClusterAPI] Cluster and control plane are ready.")
                    self.bus.emit(
                        ClusterAPIReady(
                            name=cluster_name,
                            namespace=namespace,
                            **self.run_ctx,
                        )
                    )
                    break

                if time.time() - start > timeout:
                    self.bus.emit(
                        ClusterAPITimedOut(
                            name=cluster_name,
                            namespace=namespace,
                            timeout_s=timeout,
                            **self.run_ctx,
                        )
                    )
                    raise TimeoutError(
                        f"[ClusterAPI] Cluster {cluster_name} not ready after {timeout} seconds"
                    )

                time.sleep(interval)

            log.debug("[ClusterAPI] Bootstrap completed successfully.")
            self.bus.emit(
                ClusterAPISummary(
                    status="OK",
                    name=cluster_name,
                    **self.run_ctx,
                )
            )

        except Exception as e:
            self.bus.emit(
                ClusterAPISummary(
                    status="FAILED",
                    name=cluster_name,
                    error=str(e),
                    **self.run_ctx,
                )
            )
            raise


