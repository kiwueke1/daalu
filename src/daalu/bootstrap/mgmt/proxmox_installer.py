# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/mgmt/proxmox_installer.py

from __future__ import annotations

import logging

from daalu.bootstrap.mgmt.models import MgmtClusterConfig

log = logging.getLogger("daalu")


class ProxmoxInstaller:
    """
    Installs the Cluster API Provider for Proxmox (CAPO) on an existing
    management Kubernetes cluster.

    Installation sequence:
      1. cert-manager                      — TLS certificate management (shared with other providers)
      2. CAPI core                         — Cluster API + kubeadm bootstrap/control-plane providers
      3. CAPO                              — Cluster API Provider for Proxmox VE
      4. Proxmox credentials               — Secret + CAPO config for Proxmox API access

    All operations run locally via subprocess using a kubeconfig that
    points at the remote mgmt cluster.
    """

    def __init__(self, kubeconfig_path: str, cfg: MgmtClusterConfig):
        self._kc = kubeconfig_path
        self._cfg = cfg

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def install(self) -> None:
        """
        Run the full CAPO installation.

        Not yet implemented — will be filled in as a future implementation step.
        Use --provider metal3 or --provider tinkerbell for a working provisioner.
        """
        raise NotImplementedError(
            "ProxmoxInstaller.install() is not yet implemented. "
            "Use --provider metal3 or --provider tinkerbell for a working provisioner."
        )

    # ------------------------------------------------------------------
    # Private sub-steps (stubs — implemented progressively)
    # ------------------------------------------------------------------

    def _install_cert_manager(self) -> None:
        """
        Apply the cert-manager manifest.

        Reuses the same URL as the Metal3 and Tinkerbell installers —
        cert-manager is provider-agnostic and the apply is idempotent.
        """
        raise NotImplementedError

    def _install_capi(self) -> None:
        """
        Initialise Cluster API core components (kubeadm bootstrap +
        control-plane providers) via clusterctl.

        Uses cfg.capi_version.
        """
        raise NotImplementedError

    def _install_cluster_api_provider_proxmox(self) -> None:
        """
        Install the Cluster API Provider for Proxmox (CAPO) via clusterctl.

          clusterctl init --infrastructure proxmox:<capo_version>

        CAPO manages Proxmox VMs as CAPI Machine objects, allowing Cluster API
        to provision workload cluster nodes on a Proxmox VE hypervisor.
        The capo_version field will be added to MgmtClusterConfig when this
        step is implemented.
        """
        raise NotImplementedError

    def _configure_proxmox_credentials(self) -> None:
        """
        Create the Proxmox API credentials Secret and CAPO configuration
        in the mgmt cluster.

        Requires the following fields (to be added to MgmtClusterConfig
        or a dedicated ProxmoxConfig sub-model when implemented):
          proxmox_url       — e.g. https://192.168.0.1:8006
          proxmox_token_id  — e.g. root@pam!capo
          proxmox_secret    — API token secret value (from secrets.yaml)

        The Secret is applied into the namespace where CAPO controllers run
        so they can authenticate against the Proxmox VE API.
        """
        raise NotImplementedError
