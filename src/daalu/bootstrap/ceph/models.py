# src/daalu/bootstrap/ceph/models.py

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

@dataclass
class CephHost:
    """
    A host that will be part of the Ceph cluster.
    """
    hostname: str         # ceph logical hostname, e.g., "ceph-1"
    address: str          # reachable IP/DNS used by cephadm (mon-ip & host add)
    username: str = "ubuntu"
    port: int = 22
    password: Optional[str] = None
    pkey_path: Optional[str] = None  # path to SSH private key file


@dataclass
class CephConfig:
    """
    Ceph deployment parameters.
    """
    version: str = "18.2.1"   # maps to quay.io/ceph/ceph:v<version> if image not given
    image: Optional[str] = None
    initial_dashboard_user: str = "admin"
    initial_dashboard_password: str = "admin"  # change in prod!
    apply_osds_all_devices: bool = True        # use --all-available-devices
    mgr_count: int = 2                         # desired mgr count
    mon_count: Optional[int] = None            # None → infer min(3, len(hosts))
