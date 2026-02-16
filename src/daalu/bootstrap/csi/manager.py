# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/csi/manager.py
from __future__ import annotations

from daalu.bootstrap.csi.rbd import CephRbdCsiDriver
from daalu.bootstrap.ceph.models import CephHost
from daalu.utils.ssh import open_ssh
import logging

log = logging.getLogger("daalu")


class CSIManager:
    def __init__(
        self,
        *,
        bus,
        helm,
        ceph_hosts: list[CephHost],
        connect_timeout: float = 20.0,
    ):
        self.bus = bus
        self.helm = helm
        self.ceph_hosts = ceph_hosts
        self.connect_timeout = connect_timeout

        if not ceph_hosts:
            raise RuntimeError("CSI requires at least one Ceph host")

        # First Ceph host is treated as primary
        self.primary_host = ceph_hosts[0]

    # ------------------------------------------------------------------
    def _ensure_helm(self, ssh) -> None:
        """
        Ensure Helm is installed on the remote host.
        Installs Helm into /usr/local/bin/helm if missing.
        """
        # Check if helm exists
        rc, out, err = ssh.run("command -v helm", sudo=True)
        if rc == 0:
            return  # Helm already installed

        log.debug("[csi] Helm not found on remote host, installing Helm...")

        install_cmd = (
            "set -euo pipefail; "
            "ARCH=$(uname -m); "
            "case \"$ARCH\" in "
            "  x86_64) ARCH=amd64 ;; "
            "  aarch64) ARCH=arm64 ;; "
            "  *) echo \"Unsupported arch: $ARCH\"; exit 1 ;; "
            "esac; "
            "TMP=$(mktemp -d); "
            "cd $TMP; "
            "curl -fsSL https://get.helm.sh/helm-v3.15.4-linux-${ARCH}.tar.gz -o helm.tgz; "
            "tar -xzf helm.tgz; "
            "sudo mv linux-${ARCH}/helm /usr/local/bin/helm; "
            "sudo chmod 755 /usr/local/bin/helm; "
            "cd /; rm -rf $TMP"
        )

        rc, out, err = ssh.run(install_cmd, sudo=True)
        if rc != 0:
            raise RuntimeError(f"Failed to install helm: {err or out}")

        # Verify
        rc, out, err = ssh.run("/usr/local/bin/helm version", sudo=True)
        if rc != 0:
            raise RuntimeError(f"Helm installed but not usable: {err or out}")

        log.debug(f"[csi] Helm installed successfully: {out.strip()}")

    # ------------------------------------------------------------------
    def _ensure_rbd_module(self) -> None:
        """Load the rbd kernel module on all ceph hosts."""
        for host in self.ceph_hosts:
            log.info("[csi] Loading rbd kernel module on %s...", host.hostname)
            host_ssh = open_ssh(host)
            try:
                host_ssh.run("modprobe rbd", sudo=True)
            finally:
                host_ssh.close()

    # ------------------------------------------------------------------
    def deploy(self, cfg) -> None:
        log.debug(f"csi primary host is {self.primary_host}")

        # Ensure rbd kernel module is loaded on all nodes
        self._ensure_rbd_module()

        ssh = open_ssh(
            self.primary_host,
            # connect_timeout=self.connect_timeout,
        )

        try:
            #  Ensure Helm exists BEFORE using HelmCliRunner
            self._ensure_helm(ssh)

            if cfg.driver == "rbd":
                CephRbdCsiDriver(
                    bus=self.bus,
                    helm=self.helm,
                    ssh=ssh,
                    host=self.primary_host,
                    env="workload",
                    context=cfg.kubeconfig_path,
                ).deploy(cfg)

            else:
                raise RuntimeError(f"Unsupported CSI driver: {cfg.driver}")

        finally:
            ssh.close()
