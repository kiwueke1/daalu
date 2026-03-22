# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/mgmt/models.py

from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel


class BaremetalProvider(str, Enum):
    """Bare-metal provisioning backend installed on the management cluster."""
    tinkerbell = "tinkerbell"
    metal3 = "metal3"
    proxmox = "proxmox"


class TinkerbellHardware(BaseModel):
    """A single bare-metal node registered as a Tinkerbell Hardware CR."""
    name: str
    mac: str               # MAC of the PXE-boot interface
    ip: str                # IP to assign to the node
    bmc_endpoint: str      # BMC URL, e.g. https://192.168.1.10
    bmc_username: str
    bmc_password: str
    disk: str = "/dev/sda"  # install target disk
    uefi: bool = True       # set False for legacy BIOS boot


class MgmtClusterConfig(BaseModel):
    """
    Configuration for bootstrapping a management Kubernetes cluster on a
    fresh Ubuntu machine, then installing a bare-metal provisioning stack on top.
    """

    # Bare-metal provisioning backend to install on the management cluster.
    # Drives which installer runs after kubeadm + Cilium are up.
    provider: BaremetalProvider = BaremetalProvider.tinkerbell

    # SSH access to the target machine
    host: str                               # IP or hostname of the fresh Ubuntu node
    ssh_username: str = "ubuntu"
    ssh_password: Optional[str] = None      # populated via secrets.yaml mgmt_cluster.ssh_password
    ssh_key: Optional[str] = None           # path to SSH private key (alternative to password)

    # Managed user created on bare-metal nodes during bootstrap
    managed_user: str = "builder"
    managed_user_password: Optional[str] = None  # populated via secrets.yaml mgmt_cluster.managed_user_password

    # Kubernetes
    kubernetes_version: str = "1.30"        # major.minor — patch resolved by apt
    pod_cidr: str = "172.16.0.0/16"
    service_cidr: str = "10.96.0.0/12"

    # Cilium CNI
    cilium_version: str = "1.16.0"

    # Cluster API versions
    capi_version: str = "v1.12.0"
    capm3_version: str = "v1.12.1"     # used when provider=metal3
    capt_version: str = "v0.6.0"       # used when provider=tinkerbell (CAPI provider)

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

    # Bare-metal node inventory — used when provider=tinkerbell.
    # Each entry becomes a Tinkerbell Hardware CR (equivalent of Metal3 BareMetalHost).
    hardware: list[TinkerbellHardware] = []

    # Where to save the kubeconfig locally after cluster creation
    kubeconfig_output_path: str = "~/.kube/daalu-mgmt-config"

    # Set to False to skip Harbor install (useful when Harbor already deployed)
    install_harbor: bool = True

    # Override the base URI that ironic-ipa-downloader uses to fetch the IPA
    # ramdisk.  Defaults to None (uses the image's built-in default, which is
    # https://tarballs.opendev.org/openstack/ironic-python-agent/dib).
    # Set this if that host is unreachable from your network, e.g.:
    #   ipa_baseuri: "http://192.168.0.1/ipa-mirror"
    # The downloader will fetch <ipa_baseuri>/ipa-centos9-master.tar.gz.
    ipa_baseuri: Optional[str] = None

    model_config = {"extra": "forbid"}
