# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# daalu/src/daalu/bootstrap/node/models.py

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

@dataclass
class Host:
    """
    Represents a server you will SSH into.
    """
    hostname: str                 # logical hostname to set (e.g., 'node-1')
    address: str                  # IP or DNS to connect
    username: str                 # SSH username
    port: int = 22
    password: Optional[str] = None
    pkey_path: Optional[Path] = None
    become_password: Optional[str] = None     # for sudo -S
    authorized_key_path: Optional[Path] = None  # path to a public key to install for SSH role
    # Optional per-host netplan content (if you don't use the renderer)
    netplan_content: Optional[str] = None

@dataclass
class NodeBootstrapPlan:
    """
    Choose which roles to run. Default: run them all.
    """
    run_apparmor: bool = True
    run_netplan: bool = True
    run_ssh_and_hostname: bool = True
    run_inotify_limits: bool = True
    run_istio_modules: bool = True
    run_containerd_registry: bool = True
    # Install linux-headers and blacklist nouveau so the NVIDIA GPU Operator's
    # unsigned kernel module can build and load. No-op on nodes without nouveau.
    run_nvidia_prereqs: bool = True
    # Move the provisioning Linux bridge uplink from the raw NIC to a VLAN
    # subinterface, freeing the raw NIC for OVS br-ex.  Disabled by default;
    # enable after Metal3 provisioning is complete and before OVS deployment.
    run_migrate_provisioning_bridge: bool = False

@dataclass
class NodeBootstrapOptions:
    """
    Global options for the bootstrapper.
    """
    cluster_name: str = "openstack-infra"          # used if kubeconfig_content is None
    cluster_namespace: str = "baremetal-operator-system"  # namespace where the kubeconfig secret lives
    kubeconfig_content: Optional[str] = None       # if provided, copy this to nodes; else fetch via clusterctl
    kubeconfig_remote_path: str = "/home/{username}/.kube/config"
    domain_suffix: str = "net.daalu.io"
    # netplan: if host.netplan_content is None, use renderer(host)->str if provided
    netplan_renderer: Optional[Callable[[Host], str]] = None
    netplan_dest_path: str = "/etc/netplan/01-netcfg.yaml"
    # inotify
    inotify_max_user_instances: str = "1280"
    inotify_max_user_watches: str = "655360"
    # user creation (ssh_and_hostname)
    managed_user: str = "builder"
    managed_user_password_plain: str = ""   # Required — will be hashed on remote using openssl -6
    # insecure (HTTP) container registries to configure in containerd
    # e.g. ["10.10.0.5:5000"] — nodes will be allowed to pull from these without TLS
    insecure_registries: List[str] = field(default_factory=list)
    # Provisioning bridge migration (run_migrate_provisioning_bridge)
    # Replaces the Linux provisioning bridge with an OVS internal port on br-ex,
    # freeing the raw NIC for use as the br-ex physical uplink.
    # No VLAN tags, no switch changes, no pfSense changes required.
    provisioning_bridge_nic: str = "eno1"       # NIC currently enslaved to the Linux bridge
    provisioning_bridge_name: str = "provisioning"  # Linux bridge name (becomes OVS port name)
    provisioning_ovs_bridge: str = "br-ex"      # OVS bridge to add eno1 and internal port to
