# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/temporal/schemas.py
"""
Workflow input schemas — drive the typed-form UI in temporal-console.

Each entry describes ONE registered workflow:
  - `workflow_type`: the @workflow.defn class name (matches Temporal-side)
  - `task_queue`: which worker pool runs it
  - `title`/`description`: shown in the UI
  - `fields`: ordered list of form fields with type, default, help, etc.

The UI fetches this via GET /api/registry on the temporal-console which proxies
to the worker, OR loads it from a baked JSON file. We keep the source of truth
HERE (in daalu) so it ships with every worker image.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FieldSchema:
    name: str                           # form field name → workflow input attr
    label: str                          # shown next to the input
    type: str = "string"                # string | bool | int | textarea | secret
    required: bool = False
    default: Any = None
    placeholder: str = ""
    help: str = ""
    choices: Optional[List[str]] = None  # if set, renders as <select>
    advanced: bool = False              # collapsed under "Advanced options" by default


@dataclass
class WorkflowSchema:
    workflow_type: str
    task_queue: str
    title: str
    description: str
    fields: List[FieldSchema] = field(default_factory=list)
    # For the workflow ID UX. {timestamp} substituted by UI.
    workflow_id_template: str = "{workflow_type}-{timestamp}"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

DEPLOY_SCHEMA = WorkflowSchema(
    workflow_type="DeployDaaluCloud",
    task_queue="daalu.deployments",
    title="Deploy Daalu Cloud",
    description=(
        "Provision the workload Kubernetes cluster (via Tinkerbell/CAPT or Metal3) "
        "and deploy the full OpenStack control plane. Equivalent to running "
        "`daalu deploy` from the CLI."
    ),
    fields=[
        FieldSchema(
            name="config_path",
            label="Cluster config",
            default="cluster-defs/cluster.yaml",
            required=True,
            help="Path to the cluster definition YAML inside the worker workspace.",
        ),
        FieldSchema(
            name="install",
            label="Install plan",
            default="cluster-api,nodes,ceph,csi,infrastructure,openstack",
            placeholder="cluster-api,nodes,ceph,csi,infrastructure,openstack",
            help="Comma-separated stages, or 'all'.",
        ),
        FieldSchema(
            name="managed_user",
            label="Managed user",
            placeholder="builder",
            help="Linux user created on workload nodes (defaults to mgmt_cluster.managed_user).",
        ),
        FieldSchema(
            name="managed_user_password",
            label="Managed user password",
            type="secret",
            help="Password for the managed user (defaults to value in secrets.yaml).",
        ),
        FieldSchema(
            name="ssh_key",
            label="SSH private key path",
            placeholder="~/.ssh/openstack-key",
            help="Path to private key on the worker filesystem.",
        ),
        FieldSchema(
            name="local_registry",
            label="Use local Harbor registry",
            type="bool",
            default=True,
            help="Pull images from the in-cluster Harbor registry (10.10.0.9:30003).",
        ),
        FieldSchema(
            name="mgmt_kubeconfig",
            label="Management kubeconfig",
            default="~/.kube/daalu-mgmt-config",
            help="Kubeconfig for the management cluster.",
        ),
        # ── advanced ───────────────────────────────────────────────────────
        FieldSchema(
            name="infra",
            label="Infrastructure components",
            placeholder="metallb,argocd,...",
            help="Restrict which infra components run (default: all).",
            advanced=True,
        ),
        FieldSchema(
            name="install_local_registry",
            label="Install Harbor on mgmt cluster first",
            type="bool",
            default=False,
            advanced=True,
        ),
        FieldSchema(
            name="registry_url",
            label="Override registry URL",
            placeholder="10.10.0.9:30003",
            advanced=True,
        ),
        FieldSchema(
            name="cluster_kubeconfig",
            label="Workload kubeconfig",
            placeholder="/tmp/kubeconfig-<cluster>.yaml",
            advanced=True,
        ),
        FieldSchema(
            name="context",
            label="Workload kube context",
            advanced=True,
        ),
        FieldSchema(
            name="mgmt_context",
            label="Management kube context",
            advanced=True,
        ),
        FieldSchema(
            name="cluster_name",
            label="Cluster name",
            default="openstack-infra",
            advanced=True,
        ),
        FieldSchema(
            name="cluster_namespace",
            label="Cluster namespace",
            default="default",
            advanced=True,
        ),
        FieldSchema(
            name="node_tags",
            label="Node bootstrap tags",
            placeholder="apparmor,netplan,ssh,inotify,istio",
            help="Restrict which node-bootstrap roles run.",
            advanced=True,
        ),
        FieldSchema(
            name="ssh_username",
            label="SSH username (initial)",
            placeholder="ubuntu",
            advanced=True,
        ),
        FieldSchema(
            name="ssh_password",
            label="SSH password",
            type="secret",
            advanced=True,
        ),
        FieldSchema(
            name="domain_suffix",
            label="Domain suffix",
            default="net.daalu.io",
            advanced=True,
        ),
        FieldSchema(
            name="ceph_version",
            label="Ceph version",
            default="20.2.0",
            advanced=True,
        ),
        FieldSchema(
            name="ceph_image",
            label="Ceph image override",
            advanced=True,
        ),
        FieldSchema(
            name="phase",
            label="Phase",
            choices=["", "pre_install", "helm", "post_install"],
            help="Run only one phase across all components.",
            advanced=True,
        ),
        FieldSchema(
            name="dry_run",
            label="Dry run",
            type="bool",
            default=False,
            advanced=True,
        ),
        FieldSchema(
            name="debug",
            label="Verbose logging",
            type="bool",
            default=False,
            advanced=True,
        ),
    ],
)


CLEAN_SCHEMA = WorkflowSchema(
    workflow_type="CleanDaaluCloud",
    task_queue="daalu.deployments",
    title="Tear down Daalu Cloud",
    description=(
        "Delete the workload cluster and reset the management cluster. "
        "Equivalent to `daalu clean`. Destructive — confirm before running."
    ),
    fields=[
        FieldSchema(
            name="config_path",
            label="Cluster config",
            default="cluster-defs/cluster.yaml",
            required=True,
        ),
        FieldSchema(
            name="mgmt_kubeconfig",
            label="Management kubeconfig",
            default="~/.kube/daalu-mgmt-config",
        ),
        FieldSchema(
            name="ssh_key",
            label="SSH private key path",
            placeholder="~/.ssh/openstack-key",
        ),
        FieldSchema(
            name="skip_workload_cluster",
            label="Skip workload cluster delete",
            type="bool",
            default=False,
            help="Use when the workload cluster is already gone.",
        ),
        FieldSchema(
            name="no_wait",
            label="Don't wait for deprovisioning",
            type="bool",
            default=False,
        ),
        FieldSchema(
            name="wipe_mgmt",
            label="Wipe management cluster",
            type="bool",
            default=False,
            help="Also kubeadm-reset the management node.",
        ),
        FieldSchema(name="install", label="Install plan filter", advanced=True),
        FieldSchema(name="infra", label="Infra filter", advanced=True),
        FieldSchema(name="kubeconfig", label="Workload kubeconfig", advanced=True),
        FieldSchema(name="ssh_password", label="SSH password", type="secret", advanced=True),
        FieldSchema(name="debug", label="Verbose logging", type="bool", default=False, advanced=True),
    ],
)


MIRROR_IMAGES_SCHEMA = WorkflowSchema(
    workflow_type="MirrorImages",
    task_queue="daalu.deployments",
    title="Mirror images to Harbor",
    description="Pull all required images from public registries and push to local Harbor.",
    fields=[
        FieldSchema(
            name="harbor_url",
            label="Harbor URL",
            default="10.10.0.9:30003",
            required=True,
        ),
        FieldSchema(
            name="config_path",
            label="Cluster config",
            default="cluster-defs/cluster.yaml",
        ),
    ],
)


REGISTRY: Dict[str, WorkflowSchema] = {
    s.workflow_type: s
    for s in (DEPLOY_SCHEMA, CLEAN_SCHEMA, MIRROR_IMAGES_SCHEMA)
}


def registry_as_dict() -> Dict[str, Any]:
    """Return the full registry as a JSON-serialisable dict (for the UI)."""
    return {
        "workflows": [asdict(s) for s in REGISTRY.values()],
    }


def registry_as_json() -> str:
    return json.dumps(registry_as_dict(), indent=2)
