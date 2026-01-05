# src/daalu/bootstrap/metal3/context.py
from typing import Dict, Any

from daalu.bootstrap.metal3.models import Metal3TemplateGenOptions
from daalu.bootstrap.metal3.template_defaults import metal3_default_jinja_vars
from daalu.bootstrap.metal3.image_manager import Metal3ImageManager


def build_metal3_jinja_context(
    *,
    opts: Metal3TemplateGenOptions,
    bmh_nic_names: list[str],
) -> Dict[str, Any]:
    ctx = metal3_default_jinja_vars()

    ctx.update(
        {
            # Identity
            "NAMESPACE": opts.namespace,
            "CLUSTER_NAME": opts.cluster_name,
            "KUBERNETES_VERSION": opts.kubernetes_version,

            # Counts
            "CONTROL_PLANE_MACHINE_COUNT": opts.control_plane_machine_count,
            "WORKER_MACHINE_COUNT": opts.worker_machine_count,

            # Metal3 / CAPM3
            "CAPM3RELEASE": opts.capm3_release,
            "CAPM3RELEASEBRANCH": opts.capm3_release_branch,
            "CAPM3_VERSION": opts.capm3_version,
            "IMAGE_OS": opts.image_os,

            # Networking
            "CLUSTER_APIENDPOINT_HOST": opts.control_plane_vip,
            "POD_CIDR": opts.pod_cidr,
            "SERVICE_CIDR": opts.service_cidr,

            # SSH / image
            "IMAGE_USERNAME": opts.image_username,
            "SSH_PUB_KEY_CONTENT": opts.ssh_public_key,
            "IMAGE_PASSWORD_HASH": opts.image_password_hash,
            "IMAGE_PASSWORD": opts.image_password,

            # Hardware
            "bmh_nic_names": bmh_nic_names,

            # Registry / images
            "REGISTRY": opts.registry,
            "REGISTRY_PORT": str(opts.registry_port),
            "REGISTRY_IMAGE_VERSION": opts.registry_image_version,
            "IMAGE_URL": opts.image_url,
            "IMAGE_CHECKSUM": "3f574e954edb288a536eafa037d5d7313cb7dd542f8e5993a02d7ce202512457",
            "IMAGE_CHECKSUM_TYPE": opts.image_checksum_type,
            "IMAGE_FORMAT": "raw",
        }
    )

    # ---- aliases required by clusterctl-vars.yaml ----
    ctx.update(
        {
            "EXTERNALV4_POOL_RANGE_START": ctx["BARE_METAL_V4_POOL_RANGE_START"],
            "EXTERNALV4_POOL_RANGE_END": ctx["BARE_METAL_V4_POOL_RANGE_END"],
            "EXTERNALV6_POOL_RANGE_START": ctx["BARE_METAL_V6_POOL_RANGE_START"],
            "EXTERNALV6_POOL_RANGE_END": ctx["BARE_METAL_V6_POOL_RANGE_END"],
        }
    )

    return ctx
