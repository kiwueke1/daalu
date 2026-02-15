# src/daalu/bootstrap/metal3/cluster_api_manager.py

from __future__ import annotations

from pathlib import Path
from typing import Optional, Dict

from daalu.bootstrap.metal3.manager import Metal3TemplateGenerator
from daalu.bootstrap.metal3.models import Metal3TemplateGenOptions
from daalu.bootstrap.metal3.image_manager import Metal3ImageManager
from daalu.bootstrap.metal3.helpers import (
    fetch_cluster_kubeconfig,
    wait_for_pods_running,
    wait_for_nodes_ready,
    label_crds_for_pivot,
    move_cluster_objects,
    deploy_cni,
    wait_for_cni_ready,
    update_hosts_and_inventory,
)
from daalu.bootstrap.metal3.images import resolve_image_spec

from daalu.utils.execution import ExecutionContext
from daalu.utils.shell import run_logged, run_remote_logged
from daalu.utils.logging import RunLogger

from daalu.observers.events import LifecycleEvent
from daalu.observers.dispatcher import EventBus


class Metal3ClusterAPIManager:
    """
    Metal3-backed Cluster API workflow.

    This manager is responsible for:
      - Generating Cluster API + Metal3 manifests
      - Preparing and serving Metal3 images
      - Applying cluster, control plane, and worker resources
      - Verifying cluster readiness
      - Performing Cluster API pivot and re-pivot

    All external commands (kubectl, clusterctl) are executed through
    run_logged() to ensure full stdout/stderr capture into a run-scoped log.
    """

    def __init__(
        self,
        workspace_root: Path,
        mgmt_context: Optional[str],
        *,
        bus: EventBus,
        ctx: ExecutionContext,
    ):
        self.workspace_root = workspace_root
        self.mgmt_context = mgmt_context
        self.bus = bus
        self.ctx = ctx

        # Run-scoped logger (mirrors CephManager behavior)
        self.logger = RunLogger("metal3")

    # ------------------------------------------------------------------
    # Template Generation
    # ------------------------------------------------------------------

    def generate_templates(self, cfg) -> Dict[str, Path]:
        """
        Generate Metal3-backed Cluster API manifests from templates.

        This renders:
          - Cluster
          - Metal3Cluster
          - KubeadmControlPlane
          - Metal3MachineTemplate
          - KubeadmConfigTemplate (control plane + workers)
        """
        templates_root = (
            self.workspace_root
            / cfg.cluster_api.metal3_templates_path
        ).resolve()

        if not templates_root.is_dir():
            raise RuntimeError(f"Metal3 templates root not found: {templates_root}")

        self.logger.log(f"Using Metal3 templates from {templates_root}")

        gen = Metal3TemplateGenerator(ctx=self.ctx)

        opts = Metal3TemplateGenOptions(
            cfg=cfg,
            kube_context=self.mgmt_context,
            namespace=cfg.cluster_api.metal3_namespace,
            cluster_name=cfg.cluster_api.cluster_name,
            kubernetes_version=cfg.cluster_api.kubernetes_version,
            control_plane_machine_count=cfg.cluster_api.control_plane_replicas,
            worker_machine_count=cfg.cluster_api.worker_replicas,
            temp_gen_dir=self.workspace_root / "artifacts" / "metal3" / "generated",
            crs_path=templates_root,
            capm3_release_branch=cfg.cluster_api.capm3_release_branch,
            capm3_release=cfg.cluster_api.capm3_release,
            capm3_version=cfg.cluster_api.capm3_version,
            image_os=cfg.cluster_api.image_os,
            capi_config_dir=Path.home() / ".config" / "cluster-api",
            ssh_public_key_path=Path(cfg.cluster_api.ssh_public_key_path),
            control_plane_vip=cfg.cluster_api.control_plane_vip,
            pod_cidr=cfg.cluster_api.pod_cidr,
            service_cidr=cfg.cluster_api.service_cidr,
            image_username=cfg.cluster_api.image_username,
            image_password=cfg.cluster_api.image_password,
            image_password_hash=cfg.cluster_api.image_password_hash,
            ssh_public_key=cfg.cluster_api.ssh_public_key,
            mgmt_host=cfg.cluster_api.mgmt_host,
            mgmt_user=cfg.cluster_api.mgmt_user,
            image_url=cfg.cluster_api.image_url,
        )

        return gen.generate(opts)

    # ------------------------------------------------------------------
    # Image Preparation
    # ------------------------------------------------------------------

    def prepare_images(self, cfg) -> dict:
        """
        Ensure Metal3 images exist on the management host and
        return Jinja-safe image metadata.
        """
        image_spec = resolve_image_spec(
            flavor=cfg.cluster_api.image_flavor,
            version=cfg.cluster_api.image_version,
            kubernetes_version=cfg.cluster_api.kubernetes_version,
        )

        self.logger.log(f"Preparing Metal3 image {image_spec.qcow2}")

        img_mgr = Metal3ImageManager(
            mgmt_host=cfg.cluster_api.mgmt_host,
            mgmt_user=cfg.cluster_api.mgmt_user,
            ssh_opts=[
                "-i", str(cfg.cluster_api.mgmt_ssh_key_path),
                "-o", "StrictHostKeyChecking=no",
            ],
        )

        meta = img_mgr.ensure_image(
            qcow2_name=image_spec.qcow2,
            raw_name=image_spec.raw,
        )

        return {
            "IMAGE_URL": meta.image_url(
                http_base=cfg.cluster_api.ironic_http_base,
                raw=True,
            ),
            "IMAGE_CHECKSUM": meta.checksum,
            "IMAGE_CHECKSUM_TYPE": meta.checksum_type,
            "IMAGE_FORMAT": "raw",
        }

    # ------------------------------------------------------------------
    # Manifest Application Helpers
    # ------------------------------------------------------------------

    def _kubectl_apply(self, manifest: Path, namespace: str, label: str):
        """
        Apply a Kubernetes manifest using kubectl with full logging.
        """
        cmd = ["kubectl"]
        if self.mgmt_context:
            cmd += ["--context", self.mgmt_context]
        cmd += ["apply", "-f", str(manifest), "-n", namespace]

        run_logged(cmd, logger=self.logger, label=label)

    # ------------------------------------------------------------------
    # Cluster API Apply Phases
    # ------------------------------------------------------------------

    def apply_cluster(self, paths: Dict[str, Path], namespace: str) -> None:
        """
        Apply the Cluster-level manifest.

        This creates:
          - Cluster
          - Metal3Cluster
          - Shared Secrets / ConfigMaps

        This MUST be applied before control plane or workers.
        """
        self.bus.emit(LifecycleEvent("metal3.apply.cluster", "START", "Applying Cluster manifest"))
        self._kubectl_apply(paths["cluster"], namespace, "kubectl.apply.cluster")
        self.bus.emit(LifecycleEvent("metal3.apply.cluster", "SUCCESS", "Cluster manifest applied"))

    def apply_controlplane(self, paths: Dict[str, Path], namespace: str) -> None:
        """
        Apply the control plane manifest.

        This triggers:
          - BareMetalHost consumption
          - kubeadm init on first control plane
          - kubeadm join on remaining control planes
        """
        self.bus.emit(LifecycleEvent("metal3.apply.controlplane", "START", "Applying control plane manifest"))
        self._kubectl_apply(paths["controlplane"], namespace, "kubectl.apply.controlplane")
        self.bus.emit(LifecycleEvent("metal3.apply.controlplane", "SUCCESS", "Control plane applied"))

    def apply_workers(self, paths: Dict[str, Path], namespace: str) -> None:
        """
        Apply worker MachineDeployments and templates.

        This triggers worker BareMetalHost provisioning and kubeadm join.
        """
        self.bus.emit(LifecycleEvent("metal3.apply.workers", "START", "Applying worker manifest"))
        self._kubectl_apply(paths["workers"], namespace, "kubectl.apply.workers")
        self.bus.emit(LifecycleEvent("metal3.apply.workers", "SUCCESS", "Workers applied"))

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self, cfg) -> None:
        """
        Verify that the workload cluster converges successfully.

        Steps:
          1. Fetch workload kubeconfig
          2. Deploy CNI
          3. Wait for pods and nodes
          4. Update inventory and hosts file
        """
        self.bus.emit(LifecycleEvent("metal3.verify", "START", "Verifying target cluster"))

        ns = cfg.cluster_api.metal3_namespace
        cluster = cfg.cluster_api.cluster_name
        expected = cfg.cluster_api.control_plane_count + cfg.cluster_api.worker_count

        kubeconfig = fetch_cluster_kubeconfig(
            cluster_name=cluster,
            namespace=ns,
            out_path=Path(f"/tmp/kubeconfig-{cluster}.yaml"),
            ctx=self.ctx,
        )

        if self.ctx.dry_run:
            self.bus.emit(LifecycleEvent("metal3.verify", "SUCCESS", "Dry-run: verify skipped"))
            return

        deploy_cni(kubeconfig, ctx=self.ctx, cni="cilium")
        wait_for_cni_ready(kubeconfig, ctx=self.ctx)

        wait_for_pods_running(kubeconfig, ctx=self.ctx)
        wait_for_nodes_ready(kubeconfig, expected_count=expected, ctx=self.ctx)

        update_hosts_and_inventory(
            kubeconfig=kubeconfig,
            workspace_root=self.workspace_root,
            domain_suffix="net.daalu.io",
            ctx=self.ctx,
        )

        self.bus.emit(LifecycleEvent("metal3.verify", "SUCCESS", "Cluster verified"))

    # ------------------------------------------------------------------
    # Pivot / Re-pivot
    # ------------------------------------------------------------------

    def pivot(self, cfg) -> None:
        """
        Pivot Cluster API management from the bootstrap cluster
        to the workload cluster.
        """
        self.bus.emit(LifecycleEvent("metal3.pivot", "START", "Pivoting cluster"))

        cluster = cfg.cluster_api.cluster_name
        kubeconfig = Path(f"/tmp/kubeconfig-{cluster}.yaml")

        label_crds_for_pivot()

        run_logged(
            [
                "clusterctl",
                "init",
                "--kubeconfig",
                str(kubeconfig),
                "--core",
                f"cluster-api:{cfg.cluster_api.capi_release}",
                "--bootstrap",
                f"kubeadm:{cfg.cluster_api.capi_release}",
                "--control-plane",
                f"kubeadm:{cfg.cluster_api.capi_release}",
                "--infrastructure",
                f"metal3:{cfg.cluster_api.capm3_release}",
                "-v",
                "5",
            ],
            logger=self.logger,
            label="clusterctl.init",
        )

        move_cluster_objects(
            from_kubeconfig=None,
            to_kubeconfig=kubeconfig,
            namespace=cfg.cluster_api.namespace,
        )

        if not self.ctx.dry_run:
            self.verify(cfg)

        self.bus.emit(LifecycleEvent("metal3.pivot", "SUCCESS", "Pivot completed"))

    def repivot(self, cfg) -> None:
        """
        Move Cluster API objects back to the original management cluster.
        """
        self.bus.emit(LifecycleEvent("metal3.repivot", "START", "Re-pivoting cluster"))

        cluster = cfg.cluster_api.cluster_name

        move_cluster_objects(
            from_kubeconfig=Path(f"/tmp/kubeconfig-{cluster}.yaml"),
            to_kubeconfig=Path.home() / ".kube" / "config",
            namespace=cfg.cluster_api.namespace,
        )

        self.bus.emit(LifecycleEvent("metal3.repivot", "SUCCESS", "Re-pivot completed"))
