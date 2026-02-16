# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

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
    registry: str = "10.10.0.9:5000"
    registry_port: int = 5000
    registry_image_version: str = "2.7.1"

    image_url: str = "http://10.10.0.9/UBUNTU_24.04_NODE_IMAGE_K8S_v1.33.0-raw.img"
    image_checksum: str = "61895579cbb6dc579bd406ea5dc63d148d6714afd32976b9da3ea0daf5212d5a"          # optional, but template expects it
    image_checksum_type: str = "sha256"
    image_format: str = "raw"

    mgmt_ssh_key_path: Path = Path.home() / ".ssh" / "id_ed25519"