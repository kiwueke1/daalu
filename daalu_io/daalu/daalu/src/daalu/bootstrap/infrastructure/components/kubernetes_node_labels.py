# src/daalu/bootstrap/infrastructure/components/kubernetes_node_labels.py

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from daalu.bootstrap.engine.component import InfraComponent
from daalu.cli.helper import (
    inventory_path,
    read_group_from_inventory,
)


class KubernetesNodeLabelsComponent(InfraComponent):
    """
    Apply Kubernetes node labels and remove control-plane taints.
    Mirrors kubernetes_node_labels Ansible role.
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        kubeconfig: str,
    ):
        super().__init__(
            name="kubernetes-node-labels",
            repo_name="none",
            repo_url="",
            chart="",
            version=None,
            namespace="",
            release_name="",
            local_chart_dir=Path("/tmp"),
            remote_chart_dir=Path("/tmp"),
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self.workspace_root = workspace_root
        self.wait_for_pods = False

    # --------------------------------------------------
    def post_install(self, kubectl) -> None:
        inv = inventory_path(self.workspace_root)

        controllers = {
            hostname
            for hostname, _ in read_group_from_inventory(inv, "controllers")
        }

        computes = {
            hostname
            for hostname, _ in read_group_from_inventory(inv, "computes")
        }

        all_nodes = controllers | computes

        for node in all_nodes:
            labels = self._labels_for_node(
                node=node,
                controllers=controllers,
                computes=computes,
            )

            # Patch labels
            kubectl.patch(
                api_version="v1",
                kind="Node",
                name=node,
                patch={
                    "metadata": {
                        "labels": labels,
                    }
                },
            )

            # Remove NoSchedule taint for control-plane nodes
            if node in controllers:
                print(f"Removing NoSchedule taint from controller node {node}")
                kubectl.patch(
                    api_version="v1",
                    kind="Node",
                    name=node,
                    patch={
                        "spec": {
                            "taints": [],
                        }
                    },
                )

    # --------------------------------------------------
    def _labels_for_node(
        self,
        *,
        node: str,
        controllers: set[str],
        computes: set[str],
    ) -> Dict[str, str]:
        labels: Dict[str, str] = {}

        if node in controllers:
            labels["openstack-control-plane"] = "enabled"
            labels["openvswitch"] = "enabled"

        if node in computes:
            labels["openstack-compute-node"] = "enabled"
            labels["openvswitch"] = "enabled"

        return labels
