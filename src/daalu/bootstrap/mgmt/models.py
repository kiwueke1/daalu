# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/mgmt/models.py

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class MgmtClusterConfig(BaseModel):
    """
    Configuration for bootstrapping a management Kubernetes cluster on a
    fresh Ubuntu machine, then installing Metal3 / Ironic / CAPI on top of it.
    """

    # SSH access to the target machine
    host: str                               # IP or hostname of the fresh Ubuntu node
    ssh_username: str = "ubuntu"
    ssh_password: Optional[str] = None      # populated via secrets.yaml mgmt_cluster.ssh_password
    ssh_key: Optional[str] = None           # path to SSH private key (alternative to password)

    # Kubernetes
    kubernetes_version: str = "1.30"        # major.minor — patch resolved by apt
    pod_cidr: str = "172.16.0.0/16"
    service_cidr: str = "10.96.0.0/12"

    # Cilium CNI
    cilium_version: str = "1.16.0"

    # Cluster API versions
    capi_version: str = "v1.12.0"
    capm3_version: str = "v1.12.1"

    # Ironic / provisioning network
    ironic_name: str = "daalu-ironic"
    ironic_namespace: str = "baremetal-operator-system"
    provisioning_interface: str = "ens18"   # NIC used for the provisioning network
    # Static IP to assign to provisioning_interface.  If set, daalu will configure
    # a netplan drop-in so the IP persists across reboots and will embed it in the
    # Ironic CR as externalIP/ipAddress.  Leave empty only if the IP is already
    # statically configured out-of-band.
    provisioning_ip: Optional[str] = None
    # CIDR prefix length for the provisioning network (e.g. "16" for /16)
    provisioning_prefix: str = "16"
    # IrSO dnsmasq DHCP pool for bare-metal PXE clients.  Required when
    # IrSO is responsible for DHCP on the provisioning network.
    dhcp_range_begin: Optional[str] = None
    dhcp_range_end: Optional[str] = None
    dhcp_gateway: Optional[str] = None
    dhcp_dns: str = "8.8.8.8"

    # Where to save the kubeconfig locally after cluster creation
    kubeconfig_output_path: str = "~/.kube/daalu-mgmt-config"

    # Set to False to skip Harbor install (useful when Harbor already deployed)
    install_harbor: bool = True

    model_config = {"extra": "forbid"}
