"""Source-of-truth abstraction layer.

The engine, reconciler, and approval UI all talk to this layer — never
directly to a Nautobot (or NetBox) client. Customers can BYO their own
SoT instance, or we host one for them per tenant; either way the rest
of the codebase sees a single :class:`SourceOfTruth` interface.
"""

from daalu_automation.core.sot.base import SourceOfTruth
from daalu_automation.core.sot.models import (
    Actor,
    AuthorizedKey,
    BiosAttribute,
    BootOverride,
    CloudInitUserData,
    Device,
    DeviceFacts,
    IntendedConfig,
    InterfaceConfig,
    LinuxFacts,
    NetworkFacts,
    ObservedSnapshot,
    PackagePresence,
    PowerControl,
    RedfishFacts,
    SoTRevision,
    StaticRoute,
    SysctlValue,
    VlanDefinition,
)
from daalu_automation.core.sot.nautobot import (
    NAUTOBOT_PROVIDER,
    NautobotSoT,
    NautobotUnavailable,
)

__all__ = [
    "Actor",
    "AuthorizedKey",
    "BiosAttribute",
    "BootOverride",
    "CloudInitUserData",
    "Device",
    "DeviceFacts",
    "IntendedConfig",
    "InterfaceConfig",
    "LinuxFacts",
    "NAUTOBOT_PROVIDER",
    "NautobotSoT",
    "NautobotUnavailable",
    "NetworkFacts",
    "ObservedSnapshot",
    "PackagePresence",
    "PowerControl",
    "RedfishFacts",
    "SoTRevision",
    "SourceOfTruth",
    "StaticRoute",
    "SysctlValue",
    "VlanDefinition",
]
