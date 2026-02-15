# src/daalu/bootstrap/openstack/components/openvswitch/openvswitch.py

from __future__ import annotations

from pathlib import Path

from daalu.bootstrap.engine.component import InfraComponent
import logging

log = logging.getLogger("daalu")


class OpenvSwitchComponent(InfraComponent):
    """
    Daalu Open vSwitch component.

    Deploys the openvswitch Helm chart (DaemonSet) which runs
    openvswitch-db-server and openvswitch-vswitchd on all nodes.

    Pre-install validates that LimitMEMLOCK=infinity is set for
    containerd (required for OVS to start).
    """

    def __init__(
        self,
        *,
        values_path: Path,
        assets_dir: Path,
        kubeconfig: str,
        namespace: str = "openstack",
        release_name: str = "openvswitch",
        ssh=None,
        enable_argocd: bool = False,
    ):
        super().__init__(
            name="openvswitch",
            repo_name="local",
            repo_url="",
            chart="openvswitch",
            version=None,
            namespace=namespace,
            release_name=release_name,
            local_chart_dir=assets_dir / "charts",
            remote_chart_dir=Path("/usr/local/src/openvswitch"),
            kubeconfig=kubeconfig,
            uses_helm=True,
            wait_for_pods=True,
            min_running_pods=1,
            enable_argocd=enable_argocd,
        )

        self.values_path = values_path
        self._assets_dir = assets_dir
        self._ssh = ssh
        self.wait_for_pods = True
        self.min_running_pods = 1

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------
    def assets_dir(self) -> Path:
        return self._assets_dir

    def values(self) -> dict:
        return self.load_values_file(self.values_path)

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        log.debug("[openvswitch] Starting pre-install...")

        # Label all nodes with openvswitch=enabled so the DaemonSet can schedule
        self._label_nodes(kubectl)

        # Validate LimitMEMLOCK=infinity for containerd (OVS won't start without it)
        if self._ssh:
            log.debug("[openvswitch] Checking LimitMEMLOCK for containerd...")
            rc, out, err = self._ssh.run(
                "systemctl show containerd --property LimitMEMLOCK",
                sudo=True,
            )
            if rc != 0:
                log.debug(f"[openvswitch] Warning: could not check LimitMEMLOCK: {err}")
            elif "LimitMEMLOCK=infinity" not in (out or ""):
                log.debug(f"[openvswitch] LimitMEMLOCK is not infinity (got: {(out or '').strip()}), fixing...")
                self._fix_memlock()
            else:
                log.debug("[openvswitch] LimitMEMLOCK=infinity confirmed")

        log.debug("[openvswitch] pre-install complete")

    def _label_nodes(self, kubectl):
        """Label all nodes with openvswitch=enabled (required by the DaemonSet nodeSelector)."""
        log.debug("[openvswitch] Labeling nodes with openvswitch=enabled...")
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
                f"label node {node} openvswitch=enabled --overwrite"
            )
            if rc != 0:
                raise RuntimeError(f"Failed to label node {node}: {err}")
            log.debug(f"[openvswitch] Labeled node {node}")

    def _fix_memlock(self):
        """Create systemd override for containerd to set LimitMEMLOCK=infinity."""
        override_dir = "/etc/systemd/system/containerd.service.d"
        override_file = f"{override_dir}/memlock.conf"
        override_content = "[Service]\nLimitMEMLOCK=infinity"

        log.debug("[openvswitch] Creating containerd systemd override...")
        self._ssh.run(f"mkdir -p {override_dir}", sudo=True)

        rc, _, err = self._ssh.run(
            f"cat > {override_file} << 'DAALU_EOF'\n{override_content}\nDAALU_EOF",
            sudo=True,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to write {override_file}: {err}")

        log.debug("[openvswitch] Reloading systemd and restarting containerd...")
        rc, _, err = self._ssh.run("systemctl daemon-reload", sudo=True)
        if rc != 0:
            raise RuntimeError(f"Failed to reload systemd: {err}")

        rc, _, err = self._ssh.run("systemctl restart containerd", sudo=True)
        if rc != 0:
            raise RuntimeError(f"Failed to restart containerd: {err}")

        # Verify the fix
        rc, out, err = self._ssh.run(
            "systemctl show containerd --property LimitMEMLOCK",
            sudo=True,
        )
        if rc != 0 or "LimitMEMLOCK=infinity" not in (out or ""):
            raise RuntimeError(
                f"LimitMEMLOCK still not infinity after fix: {(out or '').strip()}"
            )
        log.debug("[openvswitch] LimitMEMLOCK=infinity set successfully")
