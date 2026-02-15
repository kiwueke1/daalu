# src/daalu/temporal/models.py

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

@dataclass
class DeployRequest:
    # where to find your cluster definition
    config_path: str

    # workspace root path as string (Path is ok too, but keep it simple)
    workspace_root: str

    # high-level behavior flags
    install: Optional[str] = None          # same comma-separated format you use now
    infra: Optional[str] = None
    context: Optional[str] = None
    mgmt_context: Optional[str] = None
    cluster_name: str = "openstack-infra"
    cluster_namespace: str = "default"
    node_tags: Optional[str] = None
    ssh_username: str = "ubuntu"
    ssh_password: Optional[str] = None
    ssh_key: Optional[str] = None
    managed_user: str = "builder"
    managed_user_password: str = ""
    domain_suffix: str = "net.daalu.io"
    ceph_version: str = "17.2.6"
    ceph_image: Optional[str] = None

    # misc
    dry_run: bool = False
    debug: bool = False

@dataclass
class DeployStatus:
    phase: str
    message: str = ""
    current_stage: Optional[str] = None
    completed_stages: Optional[List[str]] = None
    error: Optional[str] = None
