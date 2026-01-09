# src/daalu/bootstrap/csi/models.py

from dataclasses import dataclass
from typing import Optional

@dataclass
class CSIConfig:
    driver: str                 # "rbd" | "local-path"
    kubeconfig_path: str
    ceph_user: str = "kube"
    ceph_pool: str = "kube"
