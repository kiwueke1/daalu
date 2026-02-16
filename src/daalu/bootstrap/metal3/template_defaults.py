# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/metal3/template_defaults.py

from typing import Dict, Any


def metal3_default_jinja_vars() -> Dict[str, Any]:
    return {
        # Kubernetes networking
        "IP_STACK": "v4",
        "POD_CIDR": "10.201.0.0/16",
        "SERVICE_CIDR": "10.96.0.0/12",

        # -----------------------------
        # Bare metal provisioning (PXE / IPA / metadata)
        # -----------------------------
        "BARE_METAL_PROVISIONER_CIDR": "16",
        "PROVISIONING_POOL_RANGE_START": "10.10.50.100",
        "PROVISIONING_POOL_RANGE_END": "10.10.50.200",

        # -----------------------------
        # External node IPs (same 10.10 network)
        # -----------------------------
        "BARE_METAL_V4_POOL_RANGE_START": "10.10.0.220",
        "BARE_METAL_V4_POOL_RANGE_END": "10.10.0.245",
        "BARE_METAL_V6_POOL_RANGE_START": "",
        "BARE_METAL_V6_POOL_RANGE_END": "",

        # Cluster API
        "CLUSTER_APIENDPOINT_PORT": 6443,
        "MAX_SURGE_VALUE": 1,
        "NODE_DRAIN_TIMEOUT": "0",

        # Diagnostics
        "CAPM3_DIAGNOSTICS_ADDRESS": "",
        "CAPM3_INSECURE_DIAGNOSTICS": "false",
        "IPAM_DIAGNOSTICS_ADDRESS": "",
        "IPAM_INSECURE_DIAGNOSTICS": "false",

        # Infra / Metal3
        "EXTERNAL_VLAN_ID": "",
        "IRONIC_ENDPOINT_BRIDGE": "provisioning",

        # Storage
        "VM_EXTRADISKS": "false",

        # -----------------------------
        # External subnet definition
        # -----------------------------
        "EXTERNAL_SUBNET_V4_HOST": "10.10.0.100",
        "EXTERNAL_SUBNET_V4_PREFIX": "16",
        "EXTERNAL_SUBNET_V6_HOST": "",
        "EXTERNAL_SUBNET_V6_PREFIX": "",
    }
