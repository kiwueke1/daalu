# src/daalu/bootstrap/openstack/components/ovn/ovn.py

from __future__ import annotations

from pathlib import Path
import json

from daalu.bootstrap.engine.component import InfraComponent
import logging

log = logging.getLogger("daalu")


class OvnComponent(InfraComponent):
    """
    Daalu OVN component (Open Virtual Network).

    Deploys the OVN Helm chart providing the SDN control and data plane:
    - ovn-northd (deployment, 3 replicas)
    - ovn-ovsdb-nb / ovn-ovsdb-sb (StatefulSets, 3 replicas each)
    - ovn-controller (DaemonSet on all nodes)
    - optionally ovn-bgp-agent (DaemonSet)

    Pre-install removes stale ovn-controller DaemonSet if it has
    an old 'type' label selector that would conflict with upgrade.
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "ovn",
        ovn_bgp_agent_enabled: bool = False,
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="ovn",
            repo_name="local",
            repo_url="",
            chart="ovn",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/ovn"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self._ovn_bgp_agent_enabled = ovn_bgp_agent_enabled
        self.wait_for_pods = True
        self.min_running_pods = 1

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------
    def assets_dir(self) -> Path:
        return self._assets_dir

    def values(self) -> dict:
        base = self.load_values_file(self.values_path)
        base.setdefault("manifests", {})
        base["manifests"]["daemonset_ovn_bgp_agent"] = self._ovn_bgp_agent_enabled
        return base

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        log.debug("[ovn] Starting pre-install...")

        # Label nodes with openstack-control-plane=enabled
        self._label_control_plane_nodes(kubectl)

        # Remove stale ovn-controller DaemonSet if it has the old 'type' label
        # selector that conflicts with upgrades
        self._cleanup_stale_controller_ds(kubectl)

        log.debug("[ovn] pre-install complete")

    def _label_control_plane_nodes(self, kubectl):
        """Label all nodes with openstack-control-plane=enabled."""
        log.debug("[ovn] Labeling nodes with openstack-control-plane=enabled...")
        rc, out, err = kubectl._run(
            "get nodes -o jsonpath={.items[*].metadata.name}"
        )
        if rc != 0:
            raise RuntimeError(f"Failed to list nodes: {err}")

        nodes = (out or "").split()
        if not nodes:
            raise RuntimeError("No nodes found in cluster")

        for node in nodes:
            rc, _, err = kubectl._run(
                f"label node {node} openstack-control-plane=enabled --overwrite"
            )
            if rc != 0:
                raise RuntimeError(f"Failed to label node {node}: {err}")
            log.debug(f"[ovn] Labeled node {node}")

    def _cleanup_stale_controller_ds(self, kubectl):
        log.debug("[ovn] Checking for stale ovn-controller DaemonSet...")
        rc, out, err = kubectl._run(
            f"get daemonset ovn-controller -n {self.namespace} -o json"
        )
        if rc != 0:
            log.debug("[ovn] No existing ovn-controller DaemonSet found")
            return

        try:
            ds = json.loads(out)
            match_labels = ds.get("spec", {}).get("selector", {}).get("matchLabels", {})
            if "type" in match_labels:
                log.debug("[ovn] Found stale 'type' label in ovn-controller selector, deleting...")
                rc, out, err = kubectl._run(
                    f"delete daemonset ovn-controller -n {self.namespace}"
                )
                if rc != 0:
                    raise RuntimeError(f"Failed to delete stale ovn-controller: {err}")
                log.debug("[ovn] Stale ovn-controller DaemonSet deleted")
            else:
                log.debug("[ovn] ovn-controller DaemonSet is clean")
        except (json.JSONDecodeError, KeyError):
            log.debug("[ovn] Could not parse ovn-controller DaemonSet, skipping cleanup")
