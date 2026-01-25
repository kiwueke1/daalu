# src/daalu/bootstrap/metal3/template_defaults.py
from typing import Dict, Any

def metal3_default_jinja_vars() -> Dict[str, Any]:
    return {
        # Networking
        "IP_STACK": "v4",
        "POD_CIDR": "10.244.0.0/16",
        "SERVICE_CIDR": "10.96.0.0/12",

        # Bare metal provisioning
        "BARE_METAL_PROVISIONER_CIDR": "24",
        "PROVISIONING_POOL_RANGE_START": "172.22.0.100",
        "PROVISIONING_POOL_RANGE_END": "172.22.0.200",

        # IPv4 / IPv6 pools
        "BARE_METAL_V4_POOL_RANGE_START": "192.168.0.220",
        "BARE_METAL_V4_POOL_RANGE_END": "192.168.0.245",
        "BARE_METAL_V6_POOL_RANGE_START": "",
        "BARE_METAL_V6_POOL_RANGE_END": "",

        # Cluster API
        "CLUSTER_APIENDPOINT_PORT": 6443,
        "MAX_SURGE_VALUE": 1,
        "NODE_DRAIN_TIMEOUT": "0",

        # Diagnostics (safe defaults)
        "CAPM3_DIAGNOSTICS_ADDRESS": "",
        "CAPM3_INSECURE_DIAGNOSTICS": "false",
        "IPAM_DIAGNOSTICS_ADDRESS": "",
        "IPAM_INSECURE_DIAGNOSTICS": "false",

        # VLAN / infra
        "EXTERNAL_VLAN_ID": "",
        "IRONIC_ENDPOINT_BRIDGE": "provisioning",

        # Storage
        "VM_EXTRADISKS": "false",

        # Optional IPv6
        "EXTERNAL_SUBNET_V4_HOST": "192.168.111.1",
        "EXTERNAL_SUBNET_V4_PREFIX": "24",
        "EXTERNAL_SUBNET_V6_HOST": "",
        "EXTERNAL_SUBNET_V6_PREFIX": "",
    }
