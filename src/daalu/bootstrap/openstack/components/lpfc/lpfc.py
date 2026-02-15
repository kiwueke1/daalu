# src/daalu/bootstrap/openstack/components/lpfc/lpfc.py

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from daalu.bootstrap.engine.component import InfraComponent
import logging

log = logging.getLogger("daalu")


# Default lpfc module parameters
DEFAULT_LPFC_PARAMS = {
    "lpfc_lun_queue_depth": 128,
    "lpfc_sg_seg_cnt": 256,
    "lpfc_max_luns": 65535,
    "lpfc_enable_fc4_type": 3,
}


class LpfcComponent(InfraComponent):
    """
    Daalu lpfc component (Emulex LightPulse Fibre Channel driver tuning).

    SSH-based host configuration — no Helm chart.

    Responsibilities:
    - Detect if the lpfc kernel module is loaded
    - Write /etc/modprobe.d/lpfc.conf with tuned parameters
    - Update initramfs and reboot if parameters changed
    """

    def __init__(
        self,
        *,
        kubeconfig: str,
        ssh,
        params: Optional[Dict[str, int]] = None,
        namespace: str = "openstack",
    ):
        super().__init__(
            name="lpfc",
            repo_name="local",
            repo_url="",
            chart="",
            version=None,
            namespace=namespace,
            release_name="lpfc",
            local_chart_dir=None,
            remote_chart_dir=None,
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self._ssh = ssh
        self._params = {**DEFAULT_LPFC_PARAMS, **(params or {})}
        self.wait_for_pods = False
        self.min_running_pods = 0
        self.enable_argocd = False

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        log.debug("[lpfc] Starting pre-install...")

        # 1) Detect if lpfc module is loaded
        rc, out, err = self._ssh.run("test -d /sys/module/lpfc", sudo=True)
        if rc != 0:
            log.debug("[lpfc] lpfc module not loaded, skipping")
            return

        log.debug("[lpfc] lpfc module detected")

        # 2) Build modprobe config line
        opts = " ".join(
            f"{k}={v}" for k, v in self._params.items()
        )
        config_content = f"options lpfc {opts}\n"

        # 3) Check current config
        rc, current, err = self._ssh.run(
            "cat /etc/modprobe.d/lpfc.conf 2>/dev/null || echo ''",
            sudo=True,
        )
        current = current.strip() if current else ""

        # 4) Check current runtime parameters
        params_changed = False
        for param, expected in self._params.items():
            rc, val, err = self._ssh.run(
                f"cat /sys/module/lpfc/parameters/{param}",
                sudo=True,
            )
            if rc != 0:
                log.debug(f"[lpfc] Could not read parameter {param}")
                params_changed = True
                continue
            actual = val.strip()
            if str(expected) != actual:
                log.debug(f"[lpfc] Parameter {param}: expected={expected}, got={actual}")
                params_changed = True

        config_changed = config_content.strip() != current

        if not config_changed and not params_changed:
            log.debug("[lpfc] Configuration already up to date")
            return

        # 5) Write config file
        log.debug("[lpfc] Writing /etc/modprobe.d/lpfc.conf...")
        self._ssh.run(
            f"echo '{config_content.strip()}' > /etc/modprobe.d/lpfc.conf",
            sudo=True,
        )

        # 6) Update initramfs
        log.debug("[lpfc] Updating initramfs...")
        rc, out, err = self._ssh.run("update-initramfs -k all -u", sudo=True)
        if rc != 0:
            raise RuntimeError(f"Failed to update initramfs: {err}")

        # 7) Reboot
        log.debug("[lpfc] Parameters changed, rebooting host...")
        self._ssh.run("reboot", sudo=True)

        log.debug("[lpfc] pre-install complete (host is rebooting)")
