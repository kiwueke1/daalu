# src/daalu/bootstrap/metal3/models.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

@dataclass(frozen=True)
class Metal3TemplateGenOptions:
    kube_context: Optional[str]
    namespace: str

    cluster_name: str
    kubernetes_version: str
    control_plane_machine_count: int
    worker_machine_count: int

    temp_gen_dir: Path
    crs_path: Path
    capm3_release_branch: str
    capm3_release: str
    capm3_version: str
    image_os: str
    capi_config_dir: Path
    ssh_public_key_path: Path
    mgmt_host: str
    mgmt_user: str
    cfg: Any

    control_plane_vip: str
    pod_cidr: str
    service_cidr: str
    image_username: str
    image_password: str
    image_password_hash: str
    ssh_public_key: str
    registry: str = "192.168.111.1:5000"
    registry_port: int = 5000
    registry_image_version: str = "2.7.1"

    image_url: str = "http://172.22.0.1/images/UBUNTU_24.04_NODE_IMAGE_K8S_v1.35.0.qcow2"
    image_checksum: str = "0e989b1a9f21d1426f0372deb139551528f276e858a3a294c2e8d09874614b85"          # optional, but template expects it
    image_checksum_type: str = "sha256"
    image_format: str = "qcow2"

    mgmt_ssh_key_path: Path = Path.home() / ".ssh" / "id_ed25519"