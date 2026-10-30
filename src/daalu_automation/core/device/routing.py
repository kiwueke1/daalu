"""Transport routing — pick the executor transport for a device + operation.

Single source of truth for "which substrate handles this change", per the
unified responsibility matrix (docs/design/nv-config-manager-integration.md
§20). Default behaviour (no feature flags) is exactly today's: each device
keeps its native ``transport``. Flags only *redirect* specific classes:

* ``config_manager_enabled`` → network-switch **config** goes to NV-CM.
* ``server_management_enabled`` → server **provisioning / power** goes to
  Tinkerbell (in-band OS config stays ``linux_ssh``; BIOS stays ``redfish``).

This module is pure (no I/O) so it's trivially unit-testable.
"""

from __future__ import annotations

from daalu_automation.core.sot.models import Device

# Native network-OS transports that NV-CM's drivers supersede when enabled.
NETWORK_TRANSPORTS: frozenset[str] = frozenset({"junos", "iosxr", "eos"})

# Transport slug for the NV-CM-backed executor (declarative switch config).
CONFIG_MANAGER_TRANSPORT = "config_manager"
# Transport slug for the Tinkerbell-backed server executor (imperative
# provisioning / BMC power).
TINKERBELL_TRANSPORT = "tinkerbell"


def is_network_device(device: Device) -> bool:
    """A switch/router whose config NV-CM owns when enabled."""
    return device.transport in NETWORK_TRANSPORTS or device.transport == CONFIG_MANAGER_TRANSPORT


def is_server_device(device: Device) -> bool:
    """A general-purpose server (Linux host or BMC-managed bare metal)."""
    return device.transport in {"linux_ssh", "redfish"} or device.platform == "linux"


def select_config_transport(
    device: Device, *, config_manager_enabled: bool
) -> str:
    """Transport for a **config-change** proposal (declarative).

    Network switches route to NV-CM when the tenant is enabled; everything
    else (Linux OS config via ``linux_ssh``, BIOS via ``redfish``, native
    NOS adapters for non-enabled tenants) keeps its existing transport.
    """
    if config_manager_enabled and is_network_device(device):
        return CONFIG_MANAGER_TRANSPORT
    return device.transport


def select_provision_transport(
    device: Device, *, server_management_enabled: bool
) -> str | None:
    """Transport for a **provisioning / power** operation (imperative).

    Returns :data:`TINKERBELL_TRANSPORT` for server hosts when the tenant
    has server management enabled, else ``None`` (the caller falls back to
    daalu's own provisioning / ``redfish``).
    """
    if server_management_enabled and is_server_device(device):
        return TINKERBELL_TRANSPORT
    return None
