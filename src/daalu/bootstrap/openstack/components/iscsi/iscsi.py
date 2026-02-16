# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/iscsi/iscsi.py

from __future__ import annotations

from daalu.bootstrap.engine.component import InfraComponent
import logging

log = logging.getLogger("daalu")


class IscsiComponent(InfraComponent):
    """
    Daalu iSCSI component (iSCSI initiator daemon).

    SSH-based host configuration — no Helm chart.

    Ensures the iscsid service is running on the host,
    which is required for Cinder iSCSI-based volume backends.
    """

    def __init__(
        self,
        *,
        kubeconfig: str,
        ssh,
        namespace: str = "openstack",
    ):
        super().__init__(
            name="iscsi",
            repo_name="local",
            repo_url="",
            chart="",
            version=None,
            namespace=namespace,
            release_name="iscsi",
            local_chart_dir=None,
            remote_chart_dir=None,
            kubeconfig=kubeconfig,
            uses_helm=False,
        )

        self._ssh = ssh
        self.wait_for_pods = False
        self.min_running_pods = 0
        self.enable_argocd = False

    # -------------------------------------------------
    # pre_install
    # -------------------------------------------------
    def pre_install(self, kubectl):
        log.debug("[iscsi] Ensuring iscsid service is started...")

        rc, out, err = self._ssh.run(
            "systemctl start iscsid",
            sudo=True,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to start iscsid: {err}")

        # Also enable it so it persists across reboots
        self._ssh.run("systemctl enable iscsid", sudo=True)

        log.debug("[iscsi] iscsid service is running")
