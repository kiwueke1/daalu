# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/mgmt/models.py

from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


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

    # SSH username used when connecting to workload nodes (e.g. to wipe their disks).
    # Workload nodes are provisioned with root access; the mgmt node uses ssh_username.
    node_ssh_username: str = "root"

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

    # Kubernetes namespace where CAPI cluster resources live (Cluster, KCP,
    # MachineDeployment, TinkerbellMachineTemplate, etc.).  Tinkerbell Hardware,
    # Template, and Workflow CRs created by CAPT are placed in this namespace,
    # so tink-controller/tink-server must have cluster-wide RBAC (ClusterRole)
    # to find them.  Must match the namespace used in the CAPI manifests under
    # assets/tinkerbell/cluster-api/*.yaml.
    cluster_namespace: str = "tinkerbell"

    # Where to save the kubeconfig locally after cluster creation
    kubeconfig_output_path: str = "~/.kube/daalu-mgmt-config"

    # Set to False to skip Harbor install (useful when Harbor already deployed)
    install_harbor: bool = True

    # Set to True to skip the bare-metal provisioning stack installation
    # (cert-manager, CAPI, CAPT/Metal3, Tinkerbell stack, hardware registration).
    # Useful when you want to install Kubernetes + Cilium only, then walk through
    # the provisioning stack manually.
    skip_provisioning_stack: bool = False

    # Override the base URI that ironic-ipa-downloader uses to fetch the IPA
    # ramdisk.  Defaults to None (uses the image's built-in default, which is
    # https://tarballs.opendev.org/openstack/ironic-python-agent/dib).
    # Set this if that host is unreachable from your network, e.g.:
    #   ipa_baseuri: "http://192.168.0.1/ipa-mirror"
    # The downloader will fetch <ipa_baseuri>/ipa-centos9-master.tar.gz.
    ipa_baseuri: Optional[str] = None

    # Temporal control plane — installed after the provisioning stack so the
    # workload-cluster deploy can be driven from the temporal-console UI.
    temporal: "TemporalConfig" = Field(default_factory=lambda: TemporalConfig())

    model_config = {"extra": "forbid"}


class TemporalConfig(BaseModel):
    """
    Temporal server + worker + UI deployed onto the management cluster.

    Set ``enabled: false`` to skip — Daalu still works fine via the CLI alone.
    """
    enabled: bool = True

    # Temporal server
    namespace: str = "temporal"
    chart_version: str = "0.62.0"           # github.com/temporalio/helm-charts
    server_image_tag: str = "1.27.0"

    # Persistence — Temporal needs a SQL backend. The chart defaults to a
    # bundled Cassandra; we set sql.enabled=false in the installer and use
    # the bundled Postgres which is lighter and easier to debug.
    storage: str = "postgresql"             # "postgresql" | "cassandra" | "mysql"

    # Daalu worker
    worker_namespace: str = "daalu"
    worker_chart_path: str = "deployments/daalu-worker/chart"
    worker_image: str = "10.10.0.9:30003/daalu/daalu-worker:latest"
    worker_replicas: int = 1
    worker_threads: int = 4

    # temporal-console UI
    console_enabled: bool = True
    console_namespace: str = "daalu"
    # Path to the temporal-console helm chart, relative to workspace_root or
    # absolute. The chart lives in a sibling repo by default.
    console_chart_path: str = "../temporal-console/chart"
    console_image: str = "10.10.0.9:30003/daalu/temporal-console:latest"
    console_brand_name: str = "Daalu Workflows"
    console_brand_subtitle: str = "operator console"
    console_host: str = "workflows.daalu.io"
    console_oidc_issuer: str = ""           # empty disables OIDC (dev only)
    console_oidc_client_id: str = "temporal-console"


# Resolve forward reference for `temporal: "TemporalConfig"`.
MgmtClusterConfig.model_rebuild()
