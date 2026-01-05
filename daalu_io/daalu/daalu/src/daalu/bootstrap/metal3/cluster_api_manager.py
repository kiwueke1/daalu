# src/daalu/bootstrap/metal3/cluster_api_manager.py
from __future__ import annotations

import subprocess
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
from daalu.utils.execution import ExecutionContext
from daalu.utils.shell import run
from daalu.observers.events import LifecycleEvent
from daalu.observers.dispatcher import EventBus
from daalu.bootstrap.metal3.images import resolve_image_spec
from daalu.bootstrap.metal3.image_manager import Metal3ImageManager

def _kubectl_apply(
    manifest: Path,
    namespace: str,
    *,
    context: str | None = None,
    ctx: ExecutionContext | None = None,
) -> None:
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += ["apply", "-f", str(manifest), "-n", namespace]

    run(cmd, ctx=ctx)


class Metal3ClusterAPIManager:
    """
    Metal3-backed Cluster API workflow.
    """

    def __init__(
        self,
        workspace_root: Path,
        mgmt_context: Optional[str] = None,
        *,
        bus: EventBus,
        ctx: ExecutionContext,
    ):
        self.workspace_root = workspace_root
        self.mgmt_context = mgmt_context
        self.bus = bus
        self.ctx = ctx

    def generate_templates(self, cfg) -> Dict[str, Path]:
        # Resolve Metal3 templates root relative to workspace
        templates_root = (
            self.workspace_root
            / cfg.cluster_api.metal3_templates_path
        ).resolve()

        if not templates_root.is_dir():
            raise RuntimeError(
                f"Metal3 templates root not found: {templates_root}"
            )

        gen = Metal3TemplateGenerator(ctx=self.ctx)

        opts = Metal3TemplateGenOptions(
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

    def download_images(self, cfg) -> None:
        mgr = Metal3ImageManager()

        items = ["controlplane", "worker"]

        for item in items:
            if cfg.cluster_api.image_os.lower() == "ubuntu":
                image = f"UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.qcow2"
                raw = f"UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0-raw.img"
            elif cfg.cluster_api.image_os.lower() == "centos":
                image = f"CENTOS_NODE_IMAGE_K8S_v1.35.0.qcow2"
                raw = f"CENTOS_NODE_IMAGE_K8S_v1.35.0-raw.img"
            else:
                raise ValueError(f"Unsupported IMAGE_OS: {cfg.cluster_api.image_os}")

            mgr.download_image(image, raw)


    def apply_cluster(self, paths: Dict[str, Path], namespace: str) -> None:
        """
        Apply the Cluster-level manifest.

        This kubectl apply creates the top-level Cluster API objects, including:
        - cluster.x-k8s.io/Cluster
        - infrastructure.cluster.x-k8s.io/Metal3Cluster
        - Associated Secrets and ConfigMaps referenced by the Cluster
            (e.g. cloud-config, credentials, endpoints)

        These objects define the workload cluster identity and infrastructure
        but do NOT yet provision any machines. This MUST be applied first.
        """
        self.bus.emit(
            LifecycleEvent(
                phase="metal3.apply.cluster",
                status="START",
                message="Applying Cluster manifest",
            )
        )

        try:
            cluster_manifest = paths["cluster"]

            run(
                [
                    "kubectl",
                    *(["--context", self.mgmt_context] if self.mgmt_context else []),
                    "apply",
                    "-f",
                    str(cluster_manifest),
                    "-n",
                    namespace,
                ],
                ctx=self.ctx,
            )

            self.bus.emit(
                LifecycleEvent(
                    phase="metal3.apply.cluster",
                    status="SUCCESS",
                    message="Cluster manifest applied",
                )
            )
        except Exception as e:
            self.bus.emit(
                LifecycleEvent(
                    phase="metal3.apply.cluster",
                    status="FAIL",
                    message=str(e),
                )
            )
            raise


    def apply_controlplane(self, paths: Dict[str, Path], namespace: str) -> None:
        """
        Apply the control plane manifest.

        This kubectl apply creates the control plane objects, including:
        - controlplane.cluster.x-k8s.io/KubeadmControlPlane
        - infrastructure.cluster.x-k8s.io/Metal3MachineTemplate
        - bootstrap.cluster.x-k8s.io/KubeadmConfigTemplate (control plane)
        - cluster.x-k8s.io/Machine resources for control plane nodes

        Applying this manifest triggers:
        - BareMetalHost consumption by Metal3
        - kubeadm init on the first control plane node
        - kubeadm join on subsequent control plane nodes

        This MUST be applied AFTER the Cluster manifest.
        """

        self.bus.emit(
            LifecycleEvent(
                phase="metal3.apply.controlplane",
                status="START",
                message="Applying control plane manifest"
            )
        )

        try:
            cp_manifest = paths["controlplane"]
        
            print(f"[metal3] Applying control plane manifest: {cp_manifest}")
            run(
                [
                    "kubectl",
                    *(["--context", self.mgmt_context] if self.mgmt_context else []),
                    "apply",
                    "-f",
                    str(cp_manifest),
                    "-n",
                    namespace,
                ],
                ctx=self.ctx,
            )

            self.bus.emit(
                LifecycleEvent(
                    phase="metal3.apply.controlplane",
                    status="SUCCESS",
                    message="Cluster manifest applied"
                )
            )
        except Exception as e:
            self.bus.emit(
                phase="metal3.apply.controlplane",
                status="FAIL",
                message=str(e)               
            )
            raise

    def apply_workers(self, paths: Dict[str, Path], namespace: str) -> None:
        """
        Apply the worker nodes manifest.

        This kubectl apply creates worker node objects, including:
        - cluster.x-k8s.io/MachineDeployment
        - cluster.x-k8s.io/MachineSet
        - cluster.x-k8s.io/Machine resources for worker nodes
        - infrastructure.cluster.x-k8s.io/Metal3MachineTemplate
        - bootstrap.cluster.x-k8s.io/KubeadmConfigTemplate (workers)

        Applying this manifest triggers:
        - BareMetalHost provisioning for worker nodes
        - kubeadm join of workers to the control plane

        This MUST be applied AFTER the control plane is available.
        """

        self.bus.emit(
            LifecycleEvent(
                phase="metal3.apply.worker",
                status="START",
                message="Applying control plane manifest"
            )
        )

        try:
            workers_manifest = paths["workers"]
        
            print(f"[metal3] Applying worker manifest: {workers_manifest}")
            run(
                [
                    "kubectl",
                    *(["--context", self.mgmt_context] if self.mgmt_context else []),
                    "apply",
                    "-f",
                    str(workers_manifest),
                    "-n",
                    namespace,
                ],
                ctx=self.ctx,
            )

            self.bus.emit(
                LifecycleEvent(
                    phase="metal3.apply.workers",
                    status="SUCCESS",
                    message="Cluster manifest applied"
                )
            )
        except Exception as e:
            self.bus.emit(
                phase="metal3.apply.workers",
                status="FAIL",
                message=str(e)               
            )
            raise

    def verify(self, cfg) -> None:
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

        # 1) Install CNI
        deploy_cni(kubeconfig, ctx=self.ctx, cni="cilium")
        wait_for_cni_ready(kubeconfig, ctx=self.ctx)

        # 2) Now cluster can converge
        wait_for_pods_running(kubeconfig, ctx=self.ctx)
        wait_for_nodes_ready(
            kubeconfig,
            expected_count=expected,
            ctx=self.ctx,
        )

        # 3) Update hosts + inventory
        update_hosts_and_inventory(
            kubeconfig=kubeconfig,
            workspace_root=self.workspace_root,
            domain_suffix="net.daalu.io",
            ctx=self.ctx,
        )

        self.bus.emit(LifecycleEvent("metal3.verify", "SUCCESS", "Cluster verified"))



    def pivot(self, cfg) -> None:
        self.bus.emit(LifecycleEvent("metal3.pivot", "START", "Pivoting cluster"))

        ns = cfg.cluster_api.namespace
        cluster = cfg.cluster_api.cluster_name
        kubeconfig = Path(f"/tmp/kubeconfig-{cluster}.yaml")

        label_crds_for_pivot()

        run(
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
            ctx=self.ctx,
        )

        move_cluster_objects(
            from_kubeconfig=None,
            to_kubeconfig=kubeconfig,
            namespace=ns,
        )

        if not self.ctx.dry_run:
            self.verify(cfg)

        self.bus.emit(LifecycleEvent("metal3.pivot", "SUCCESS", "Pivot completed"))


    def repivot(self, cfg) -> None:
        self.bus.emit(LifecycleEvent("metal3.repivot", "START", "Re-pivoting cluster"))

        ns = cfg.cluster_api.namespace
        cluster = cfg.cluster_api.cluster_name

        move_cluster_objects(
            from_kubeconfig=Path(f"/tmp/kubeconfig-{cluster}.yaml"),
            to_kubeconfig=Path.home() / ".kube" / "config",
            namespace=ns,
        )

        self.bus.emit(LifecycleEvent("metal3.repivot", "SUCCESS", "Re-pivot completed"))


