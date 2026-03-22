# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/openvswitch/openvswitch.py

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

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
        # Provisioning bridge migration (optional).
        # When node_hosts is set, post_install migrates the Metal3 provisioning
        # Linux bridge to an OVS internal port on br-ex, freeing the NIC.
        node_hosts: Optional[List] = None,
        provisioning_bridge_nic: str = "eno1",
        provisioning_bridge_name: str = "provisioning",
        provisioning_ovs_bridge: str = "br-ex",
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
        self._node_hosts = node_hosts or []
        self._provisioning_bridge_nic = provisioning_bridge_nic
        self._provisioning_bridge_name = provisioning_bridge_name
        self._provisioning_ovs_bridge = provisioning_ovs_bridge

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

    # -------------------------------------------------
    # post_install — provisioning bridge migration
    # -------------------------------------------------

    def post_install(self, kubectl) -> None:
        """
        After the OVS DaemonSet is running, migrate the Metal3 provisioning
        Linux bridge to an OVS internal port on br-ex, freeing the physical
        NIC (eno1) to act as the br-ex uplink.

        This runs per node only when node_hosts was supplied at construction
        time.  It is idempotent — nodes where eno1 is already free are
        skipped silently.

        WHY HERE (not in node bootstrap)
        ---------------------------------
        The migration requires br-ex to already exist in OVSDB.  Node
        bootstrap runs BEFORE the OVS DaemonSet is deployed, so running the
        migration there would always fail.  post_install runs after Helm has
        deployed the DaemonSet and wait_for_pods has confirmed it is Ready.

        WHY kubectl exec FOR ovs-vsctl
        --------------------------------
        The OpenStack-Helm OVS DaemonSet does NOT install ovs-vsctl on the
        host OS — it is only inside the pod.  We exec into the vswitchd
        container to run ovs-vsctl, which talks to the OVS daemon via the
        shared /run/openvswitch socket.  Host-level steps (ip addr, systemd,
        netplan) are done over SSH as usual.
        """
        if not self._node_hosts:
            log.debug("[openvswitch] node_hosts not set — skipping provisioning bridge migration")
            return

        # Lazy import to avoid circular dependency at module load time
        from daalu.bootstrap.node.ssh_bootstrapper import SshBootstrapper

        bootstrapper = SshBootstrapper()

        for host in self._node_hosts:
            log.info("[openvswitch] Checking provisioning bridge on %s...", host.hostname)
            try:
                self._migrate_node_provisioning_bridge(kubectl, bootstrapper, host)
            except Exception as exc:
                log.error(
                    "[openvswitch] Provisioning bridge migration FAILED on %s: %s",
                    host.hostname, exc,
                )
                raise

    def _migrate_node_provisioning_bridge(self, kubectl, bootstrapper, host) -> None:
        """
        Migrate one node's provisioning Linux bridge to an OVS internal port.

        Steps:
          1. SSH: idempotency check — skip if nic already free
          2. SSH: save provisioning IP from Linux bridge
          3. kubectl exec: ensure br-ex exists (auto_bridge_add may still be starting)
          4. SSH: detach nic from Linux bridge
          5. kubectl exec: add nic as physical uplink to br-ex
          6. SSH: delete Linux bridge (IP lost ~1 s)
          7. kubectl exec: create OVS internal port with same name as bridge
          8. SSH: bring port up + restore IP
          9. SSH: install systemd oneshot service (IP persistence on reboot)
         10. SSH: remove Linux bridge definition from netplan
        """
        nic = self._provisioning_bridge_nic      # eno1
        bridge = self._provisioning_bridge_name  # provisioning
        ovs_br = self._provisioning_ovs_bridge   # br-ex

        h = bootstrapper._connect(host)
        try:
            # ── step 1: idempotency ───────────────────────────────────────────
            rc, out, _ = bootstrapper._run(h, f"ip link show dev {nic}", sudo=False)
            if f"master {bridge}" not in out:
                log.info(
                    "[openvswitch] %s already free on %s — migration skipped",
                    nic, host.hostname,
                )
                return

            # ── step 2: save provisioning IP ──────────────────────────────────
            _, prov_ip_raw, _ = bootstrapper._run(
                h,
                f"ip -o addr show dev {bridge} | awk '/inet /{{print $4; exit}}'",
                sudo=False,
            )
            prov_ip = prov_ip_raw.strip()
            if not prov_ip:
                raise RuntimeError(
                    f"[{host.hostname}] Could not read IP from {bridge} — "
                    "is the provisioning bridge up?"
                )

            log.info(
                "[openvswitch] Migrating %s (IP %s) on %s to OVS internal port on %s ...",
                bridge, prov_ip, host.hostname, ovs_br,
            )

            # Find the OVS vswitchd pod on this node for kubectl exec.
            # host.hostname is the FQDN (e.g. node.net.daalu.io) but the
            # Kubernetes node name is just the short name (e.g. node).
            k8s_node = host.hostname.split(".")[0]
            rc_pod, pod_raw, pod_err = kubectl._run(
                f"get pod -n {self.namespace}"
                f" -l application=openvswitch,component=server"
                f" --field-selector spec.nodeName={k8s_node}"
                f" -o jsonpath={{.items[0].metadata.name}}"
            )
            pod_name = pod_raw.strip().strip("'\"") if rc_pod == 0 else ""
            if not pod_name:
                raise RuntimeError(
                    f"[{host.hostname}] Could not find OVS pod on node '{k8s_node}': "
                    f"{pod_err or 'no pod matching application=openvswitch,component=server'}"
                )

            # Wait for the vswitchd container to be ready before exec-ing.
            kubectl._run(
                f"wait pod -n {self.namespace} {pod_name}"
                f" --for=condition=Ready --timeout=120s"
            )

            def _ovs(cmd: str) -> None:
                """Execute an OVS command inside the vswitchd container."""
                rc_x, out_x, err_x = kubectl._run(
                    f"exec -n {self.namespace} {pod_name}"
                    f" -c openvswitch-vswitchd"
                    f" -- {cmd}"
                )
                if rc_x != 0:
                    raise RuntimeError(
                        f"kubectl exec ovs cmd failed on {host.hostname}: {err_x or out_x}"
                    )

            # ── step 3: ensure br-ex exists (auto_bridge_add may be slow) ─────
            _ovs(f"ovs-vsctl --may-exist add-br {ovs_br}")

            # ── step 4: detach nic from Linux bridge ──────────────────────────
            bootstrapper._run(h, f"ip link set {nic} nomaster", sudo=True)

            # ── step 5: add nic to br-ex ──────────────────────────────────────
            _ovs(f"ovs-vsctl --may-exist add-port {ovs_br} {nic}")

            # ── step 6: delete Linux bridge ────────────────────────────────────
            bootstrapper._run(h, f"ip link del {bridge}", sudo=True)

            # ── step 7: create OVS internal port ──────────────────────────────
            _ovs(
                f"ovs-vsctl --may-exist add-port {ovs_br} {bridge}"
                f" -- set Interface {bridge} type=internal"
            )

            # ── step 8: restore IP ─────────────────────────────────────────────
            bootstrapper._run(h, f"ip link set {bridge} up", sudo=True)
            bootstrapper._run(h, f"ip addr add {prov_ip} dev {bridge}", sudo=True)

            log.info(
                "[openvswitch] Live migration done on %s — %s IP %s restored",
                host.hostname, bridge, prov_ip,
            )

            # ── step 9: systemd service for IP persistence across reboots ──────
            bootstrapper._install_ovs_provisioning_ip_service(h, host, bridge, prov_ip)

            # ── step 10: remove Linux bridge from netplan ─────────────────────
            bootstrapper._remove_provisioning_bridge_from_netplan(h, host, bridge)

        finally:
            bootstrapper._close(h)
