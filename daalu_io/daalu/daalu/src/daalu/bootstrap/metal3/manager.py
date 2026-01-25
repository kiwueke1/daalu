# src/daalu/bootstrap/metal3/manager.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from daalu.execution.runner import CommandRunner

from daalu.bootstrap.metal3.bmh import list_bmhs, extract_bmh_nic_names, get_node_nics_from_cfg
from daalu.bootstrap.metal3.clusterctl_config import upsert_clusterctl_vars_block
from daalu.bootstrap.metal3.models import Metal3TemplateGenOptions
from daalu.bootstrap.metal3.templates import (
    resolve_release_templates_dir,
    render_jinja_templates,
    render_jinja_text,
)
from daalu.bootstrap.metal3.template_defaults import metal3_default_jinja_vars
from daalu.bootstrap.metal3.context import build_metal3_jinja_context


@dataclass
class Metal3TemplateGenerator:
    ctx: Any  # expected to carry logger + dry_run

    def __post_init__(self) -> None:
        self.runner = CommandRunner(
            logger=getattr(self.ctx, "logger", None),
            dry_run=getattr(self.ctx, "dry_run", False),
        )

    def generate(
        self,
        opts: Metal3TemplateGenOptions,
        *,
        extra_context: dict[str, Any] | None = None,
    ) -> Dict[str, Path]:
        # ------------------------------------------------------------------
        # 1) Get BMHs + extract NIC names
        # ------------------------------------------------------------------
        bmhs = list_bmhs(namespace=opts.namespace, kube_context=opts.kube_context)
        if not bmhs:
            raise RuntimeError(
                f"No BareMetalHosts found in namespace={opts.namespace}"
            )

        # Pick the BMH used for control-plane template rendering
        bmh = bmhs[0]
        bmh_name = bmh.name

        # Source of truth: cluster.yaml
        bmh_nic_names = get_node_nics_from_cfg(
            opts.cfg,
            bmh_name,
        )

       #bmh0 = bmhs[0].raw
        #bmh_nic_names = extract_bmh_nic_names(bmh0)

        ssh_pub_key = (
            opts.ssh_public_key_path
            .expanduser()
            .read_text(encoding="utf-8")
            .strip()
        )

        # ------------------------------------------------------------------
        # 2) Ensure temp output dir
        # ------------------------------------------------------------------
        opts.temp_gen_dir.mkdir(parents=True, exist_ok=True)

        # ------------------------------------------------------------------
        # 3) Resolve and validate Metal3 release templates
        # ------------------------------------------------------------------
        release_dir = resolve_release_templates_dir(
            templates_root=opts.crs_path,
            release_branch=opts.capm3_release_branch,
        )

        # ------------------------------------------------------------------
        # 4) Deploy clusterctl-vars.yaml into clusterctl.yaml
        # ------------------------------------------------------------------
        clusterctl_vars_path = release_dir / "clusterctl-vars.yaml"
        raw_block_text = clusterctl_vars_path.read_text(encoding="utf-8")

        jinja_ctx = {
            **metal3_default_jinja_vars(),
            **build_metal3_jinja_context(
                opts=opts,
                bmh_nic_names=bmh_nic_names,
            ),
            "SSH_PUB_KEY_CONTENT": ssh_pub_key,
        }

        if extra_context:
            jinja_ctx.update(extra_context)

        clusterctl_yaml_path = opts.capi_config_dir / "clusterctl.yaml"

        rendered_block_text = render_jinja_text(
            template_text=raw_block_text,
            context=jinja_ctx,
        )

        upsert_clusterctl_vars_block(
            clusterctl_yaml_path,
            rendered_block_text,
        )

        # ------------------------------------------------------------------
        # 5) Render cluster templates into clusterctl overrides
        # ------------------------------------------------------------------
        overrides_dir = (
            opts.capi_config_dir
            / "overrides"
            / "infrastructure-metal3"
            / opts.capm3_release
        )

        REQUIRED_METAL3_VARS = {
            "NAMESPACE",
            "CLUSTER_NAME",
            "KUBERNETES_VERSION",
            "CONTROL_PLANE_MACHINE_COUNT",
            "WORKER_MACHINE_COUNT",
            "IMAGE_USERNAME",
            "SSH_PUB_KEY_CONTENT",
            "CLUSTER_APIENDPOINT_HOST",
            "POD_CIDR",
            "SERVICE_CIDR",
        }

        missing = REQUIRED_METAL3_VARS - jinja_ctx.keys()
        if missing:
            raise RuntimeError(
                f"Missing required Metal3 template variables: {sorted(missing)}"
            )

        render_jinja_templates(
            templates_root=release_dir,
            src_files=[
                "cluster-template-cluster.yaml",
                "cluster-template-controlplane.yaml",
                "cluster-template-workers.yaml",
            ],
            dst_dir=overrides_dir,
            context=jinja_ctx,
        )

        # ------------------------------------------------------------------
        # 6) Generate final manifests via clusterctl
        # ------------------------------------------------------------------
        out_paths: Dict[str, Path] = {}

        def gen(item: str) -> None:
            src = overrides_dir / f"cluster-template-{item}.yaml"
            out = (
                opts.temp_gen_dir
                / f"{opts.capm3_version}_{item}_{opts.image_os}.yaml"
            )

            cmd = [
                "clusterctl",
                "generate",
                "cluster",
                opts.cluster_name,
                "--from",
                str(src),
                "--kubernetes-version",
                opts.kubernetes_version,
                f"--control-plane-machine-count={opts.control_plane_machine_count}",
                f"--worker-machine-count={opts.worker_machine_count}",
                f"--target-namespace={opts.namespace}",
            ]

            result = self.runner.run(
                cmd,
                capture_output=True,
                check=True,
            )

            out.write_text(result.stdout or "", encoding="utf-8")
            out_paths[item] = out

        gen("cluster")
        gen("controlplane")
        gen("workers")

        return out_paths

