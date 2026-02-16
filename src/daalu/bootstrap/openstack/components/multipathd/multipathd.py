# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/multipathd/multipathd.py

from __future__ import annotations

from pathlib import Path
from typing import Optional

from daalu.bootstrap.engine.component import InfraComponent
import logging

log = logging.getLogger("daalu")


MULTIPATH_CONF = """\
defaults {{
    user_friendly_names {user_friendly_names}
    find_multipaths yes
}}

devices {{
    device {{
        vendor                      "NVME"
        product                     "Pure Storage FlashArray"
        path_selector               "queue-length 0"
        path_grouping_policy        group_by_prio
        prio                        ana
        failback                    immediate
        fast_io_fail_tmo            10
        user_friendly_names         no
        no_path_retry               0
        features                    0
        dev_loss_tmo                60
        find_multipaths             yes
    }}
    device {{
        vendor                   "PURE"
        product                  "FlashArray"
        path_selector            "service-time 0"
        hardware_handler         "1 alua"
        path_grouping_policy     group_by_prio
        prio                     alua
        failback                 immediate
        path_checker             tur
        fast_io_fail_tmo         10
        user_friendly_names      no
        no_path_retry            0
        features                 0
        dev_loss_tmo             600
        find_multipaths          yes
    }}
}}

blacklist {{
      devnode "^pxd[0-9]*"
      devnode "^pxd*"
      device {{
        vendor "VMware"
        product "Virtual disk"
      }}
}}
"""


class MultipathdComponent(InfraComponent):
    """
    Daalu multipathd component (DM-Multipath configuration).

    SSH-based host configuration — no Helm chart.

    Configures Linux multipath I/O for SAN storage (Fibre Channel / iSCSI),
    providing path redundancy and load balancing. Includes optimized settings
    for Pure Storage FlashArray devices.

    Responsibilities:
    - Install multipath-tools and kpartx from backports PPA
    - Write /etc/multipath.conf with device-specific settings
    - Restart multipathd service when config changes
    """

    def __init__(
        self,
        *,
        kubeconfig: str,
        ssh,
        user_friendly_names: bool = False,
        repository: str = "ppa:vexxhost/backports",
        namespace: str = "openstack",
    ):
        super().__init__(
            name="multipathd",
            repo_name="local",
            repo_url="",
            chart="",
            version=None,
            namespace=namespace,
            release_name="multipathd",
            local_chart_dir=None,
            remote_chart_dir=None,
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self._ssh = ssh
        self._user_friendly_names = user_friendly_names
        self._repository = repository
        self.wait_for_pods = False
        self.min_running_pods = 0
        self.enable_argocd = False

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        log.debug("[multipathd] Starting pre-install...")

        # 1) Add backports PPA (Ubuntu Jammy)
        log.debug("[multipathd] Adding backports PPA...")
        rc, out, err = self._ssh.run(
            f"add-apt-repository -y '{self._repository}'",
            sudo=True,
        )
        if rc != 0:
            log.debug(f"[multipathd] Warning: PPA add returned {rc}: {err}")

        # 2) Pin multipath packages to PPA
        log.debug("[multipathd] Pinning multipath packages to PPA...")
        pin_content = (
            'Package: multipath-tools kpartx\n'
            'Pin: origin "ppa.launchpad.net"\n'
            'Pin-Priority: 1001'
        )
        self._ssh.run(
            f"cat > /etc/apt/preferences.d/99-multipath-ppa.pref << 'DAALU_EOF'\n{pin_content}\nDAALU_EOF",
            sudo=True,
        )

        # 3) Update apt cache
        self._ssh.run("apt-get update -qq", sudo=True)

        # 4) Unhold packages if previously held
        self._ssh.run(
            "apt-mark unhold multipath-tools kpartx 2>/dev/null || true",
            sudo=True,
        )

        # 5) Install multipath-tools and kpartx
        log.debug("[multipathd] Installing multipath-tools and kpartx...")
        rc, out, err = self._ssh.run(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y multipath-tools kpartx",
            sudo=True,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to install multipath packages: {err}")

        # 6) Write /etc/multipath.conf
        ufn = "yes" if self._user_friendly_names else "no"
        config = MULTIPATH_CONF.format(user_friendly_names=ufn)

        # Check if config changed
        rc, current, _ = self._ssh.run(
            "cat /etc/multipath.conf 2>/dev/null || echo ''",
            sudo=True,
        )
        current = current.strip() if current else ""

        if config.strip() == current:
            log.debug("[multipathd] Configuration already up to date")
            return

        log.debug("[multipathd] Writing /etc/multipath.conf...")
        self._ssh.run(
            f"cat > /etc/multipath.conf << 'DAALU_EOF'\n{config}DAALU_EOF",
            sudo=True,
        )

        # 7) Restart multipathd
        log.debug("[multipathd] Restarting multipathd service...")
        rc, out, err = self._ssh.run(
            "systemctl restart multipathd",
            sudo=True,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to restart multipathd: {err}")

        log.debug("[multipathd] pre-install complete")
