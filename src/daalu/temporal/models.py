# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/temporal/models.py
"""
Dataclasses passed across the Temporal workflow / activity boundary.

Everything must be JSON-serialisable — these end up in the workflow history
and on the wire between worker and the Temporal frontend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class DeployRequest:
    """
    Single input dataclass for the DeployDaaluCloud workflow.
    Mirrors the args of `daalu deploy` so the UI form maps 1-to-1 to the CLI.

    Path-like fields are passed as strings (Temporal payloads are JSON).
    """

    # required
    config_path: str = "cluster-defs/cluster.yaml"
    workspace_root: str = "/workspace"

    # install plan
    install: Optional[str] = None       # e.g. "cluster-api,nodes,ceph,csi,infrastructure,openstack"
    infra: Optional[str] = None         # comma-separated infra components

    # registry
    install_local_registry: bool = False
    local_registry: bool = False
    registry_url: Optional[str] = None

    # kubeconfigs / contexts
    mgmt_kubeconfig: Optional[str] = None
    cluster_kubeconfig: Optional[str] = None
    context: Optional[str] = None
    mgmt_context: Optional[str] = None

    # cluster identity
    cluster_name: str = "openstack-infra"
    cluster_namespace: str = "default"

    # node bootstrap
    node_tags: Optional[str] = None
    ssh_username: Optional[str] = None
    ssh_password: Optional[str] = None
    ssh_key: Optional[str] = None
    managed_user: Optional[str] = None
    managed_user_password: Optional[str] = None

    # misc deploy options
    domain_suffix: str = "net.daalu.io"
    ceph_version: str = "20.2.0"
    ceph_image: Optional[str] = None
    dry_run: bool = False
    debug: bool = False
    phase: Optional[str] = None         # pre_install | helm | post_install


@dataclass
class CleanRequest:
    """Input for the CleanDaaluCloud workflow (mirrors `daalu clean`)."""
    config_path: str = "cluster-defs/cluster.yaml"
    workspace_root: str = "/workspace"
    install: Optional[str] = None
    infra: Optional[str] = None
    kubeconfig: Optional[str] = None
    mgmt_kubeconfig: Optional[str] = None
    ssh_key: Optional[str] = None
    ssh_password: Optional[str] = None
    skip_workload_cluster: bool = False
    no_wait: bool = False
    wipe_mgmt: bool = False
    yes: bool = True
    debug: bool = False


@dataclass
class MirrorImagesRequest:
    """Input for the MirrorImages workflow (mirrors `daalu mirror-images`)."""
    harbor_url: str
    config_path: str = "cluster-defs/cluster.yaml"
    workspace_root: str = "/workspace"


# ---------------------------------------------------------------------------
# Activity input/output (per-stage)
# ---------------------------------------------------------------------------

@dataclass
class StageInput:
    """
    Input passed to a single deploy_stage activity. Subset of DeployRequest
    plus the stage name to execute.
    """
    stage: str                          # e.g. "cluster-api", "ceph", "openstack"
    request: DeployRequest


@dataclass
class StageResult:
    """Result of a single stage activity."""
    stage: str
    success: bool
    exit_code: int
    duration_secs: float
    log_tail: str = ""                  # last ~4KB of combined stdout+stderr
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Progress (queried by the UI)
# ---------------------------------------------------------------------------

@dataclass
class StageProgress:
    """Per-stage progress entry exposed via the workflow `progress` query."""
    name: str
    status: str = "pending"             # pending | running | succeeded | failed | skipped
    started_at: Optional[str] = None    # ISO-8601
    ended_at: Optional[str] = None
    log_lines: List[str] = field(default_factory=list)  # streamed via heartbeat
    error: Optional[str] = None


@dataclass
class WorkflowProgress:
    """
    Top-level progress payload returned by the workflow's `progress` query.
    The UI polls this every ~2s and renders a collapsible card per stage.
    """
    workflow_id: str = ""
    phase: str = "PENDING"              # PENDING | RUNNING | SUCCEEDED | FAILED
    message: str = ""
    current_stage: Optional[str] = None
    stages: List[StageProgress] = field(default_factory=list)
    error: Optional[str] = None
