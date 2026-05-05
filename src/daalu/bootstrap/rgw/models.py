# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/rgw/models.py
from dataclasses import dataclass
from typing import Optional


@dataclass
class RGWConfig:
    """
    Configuration for deploying a Ceph RGW (Rados Gateway) daemon.

    Defaults match the typical converged-cluster setup:
    - one RGW daemon on the primary ceph host
    - port 7480 (ceph's stock RGW port; avoids clashes with 8080 used by
      horizon/keystone on the same node)
    - no default user created (muse/other apps create their own via
      radosgw-admin — opt in by setting default_user_id)
    """

    realm: str = "default"
    placement_count: int = 1
    port: int = 7480
    ready_timeout_secs: int = 300

    default_user_id: Optional[str] = None
    default_user_display_name: Optional[str] = None
