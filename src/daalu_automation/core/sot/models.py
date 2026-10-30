"""Pydantic models for the source-of-truth (SoT) layer.

The :class:`SourceOfTruth` ABC trades in these transport-agnostic
shapes so a future ``NetBoxSoT`` (or any other backend) can populate
the same things ``NautobotSoT`` does without leaking vendor types into
the engine / reconciler / UI.

The Linux-facts schema is deliberately split into one keyed sub-object
per fact (``hostname``, ``authorized_keys``, ``sysctl``, ``packages``,
``cloud_init``) so the diff engine and renderer can grow new facts as
data, not code.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ── Linux-facts schema ────────────────────────────────────────────────


class AuthorizedKey(BaseModel):
    user: str
    key: str


class SysctlValue(BaseModel):
    name: str
    value: str


class PackagePresence(BaseModel):
    name: str
    state: Literal["present", "absent"] = "present"


class CloudInitUserData(BaseModel):
    # v1 keeps cloud-init opaque: any non-empty content is written to
    # /var/lib/cloud/seed/nocloud/user-data verbatim. Empty content
    # means the adapter manages nothing.
    content: str = ""


class LinuxFacts(BaseModel):
    hostname: str | None = None
    authorized_keys: list[AuthorizedKey] = Field(default_factory=list)
    sysctl: list[SysctlValue] = Field(default_factory=list)
    packages: list[PackagePresence] = Field(default_factory=list)
    cloud_init: CloudInitUserData | None = None


# ── Redfish-facts schema ──────────────────────────────────────────────


class BiosAttribute(BaseModel):
    name: str
    # Redfish typically returns BIOS attribute values as strings,
    # booleans, or integers. We normalise to string so the diff is
    # always textual; the adapter coerces back on write.
    value: str


class BootOverride(BaseModel):
    # Per the Redfish DSP0266 enums (Boot.BootSourceOverrideTarget /
    # BootSourceOverrideEnabled). We bias toward the conservative
    # default of "leave alone" so a missing fact never accidentally
    # reboots a server into PXE.
    target: Literal[
        "None", "Pxe", "Hdd", "Cd", "Usb", "BiosSetup", "UefiTarget",
        "UefiHttp", "UefiShell", "Floppy", "RemoteDrive", "Utilities",
        "Diags",
    ] = "None"
    enabled: Literal["Disabled", "Once", "Continuous"] = "Disabled"


class PowerControl(BaseModel):
    # Desired *stable* state. The transition method (graceful vs force
    # restart) is an execute-time concern, not a fact, so it lives on
    # the adapter rather than here.
    desired_state: Literal["On", "Off"] | None = None


class RedfishFacts(BaseModel):
    # BIOS attribute set — the adapter only manages the keys you list
    # here. Unmanaged BIOS attrs on the device are left untouched (same
    # contract LinuxFacts uses for sysctl / packages).
    bios_attributes: list[BiosAttribute] = Field(default_factory=list)
    boot_override: BootOverride | None = None
    power: PowerControl | None = None


# ── Network-facts schema (Junos / IOS-XR / Arista EOS) ───────────────


class InterfaceConfig(BaseModel):
    # ``name`` is whatever the device calls it natively — Cisco
    # "GigabitEthernet0/0/0/1", Juniper "ge-0/0/0", Arista "Ethernet1".
    # The renderer keys on this string verbatim so a typo there shows
    # up as a different interface, never silently merges.
    name: str
    description: str | None = None
    enabled: bool = True
    # ``None`` means "leave whatever the device defaults to" — same
    # contract LinuxFacts uses for the sysctl set. Setting an explicit
    # value here is the only way to manage that key.
    mtu: int | None = None
    # CIDR form, e.g. "10.0.0.1/24". A bare-host address ("10.0.0.1")
    # is accepted but the adapter pushes /32 to the device — be
    # explicit in intent if a wider mask is desired.
    ipv4_address: str | None = None
    # L2 access-port VLAN. Mutually exclusive with ipv4_address in
    # practice (a port is either L2 or L3), but we don't enforce that
    # here — let the device reject inconsistent intent.
    vlan_access: int | None = None


class VlanDefinition(BaseModel):
    # Permissive on range — some teams reserve extended ranges. Device
    # rejects out-of-band IDs; surfacing here would just add friction.
    vlan_id: int
    name: str | None = None


class StaticRoute(BaseModel):
    prefix: str  # CIDR, e.g. "10.0.0.0/24"
    next_hop: str
    # ``None`` = default (global) VRF. Anything else is a named VRF on
    # the device; the adapter writes the route into that VRF's table.
    vrf: str | None = None


class NetworkFacts(BaseModel):
    """Narrow shape for v1 network-device intent.

    Manages only hostname / interfaces / VLANs / static routes. BGP,
    OSPF, ACLs, route-maps, QoS, MPLS, multicast — all explicitly out
    of scope for v1; each adds enough surface area to warrant its own
    follow-up. The same "only manage what's in intent" rule applies:
    unmanaged interfaces / VLANs / routes on the device are left
    untouched.
    """

    hostname: str | None = None
    interfaces: list[InterfaceConfig] = Field(default_factory=list)
    vlans: list[VlanDefinition] = Field(default_factory=list)
    static_routes: list[StaticRoute] = Field(default_factory=list)


# ── Discriminated facts ──────────────────────────────────────────────
# Plain union — pydantic *does not* auto-deserialize this anywhere; the
# NautobotSoT layer chooses which type to instantiate per device based
# on the device's transport string. Keeping it as a Union (and not a
# discriminated union with a `kind` field) avoids requiring existing
# Nautobot Config Context blobs to grow a discriminator — the device's
# transport is already the discriminator.
DeviceFacts = LinuxFacts | RedfishFacts | NetworkFacts


# ── SoT primitives ────────────────────────────────────────────────────


class Device(BaseModel):
    """SoT-native device identity.

    ``id`` is opaque — for :class:`NautobotSoT` it is the Nautobot
    Device UUID string. We deliberately do **not** carry a SQLAlchemy
    FK to it: device rows live in the SoT, not in this database.
    """

    id: str
    name: str
    primary_ip: str | None = None
    platform: str = "linux"
    transport: str = "linux_ssh"
    tags: list[str] = Field(default_factory=list)
    nautobot_url: str | None = None
    # Free-form bag for adapter-specific extras (Nautobot custom fields,
    # NetBox custom fields, etc.). Resolvers downstream may read e.g.
    # ``extra["ssh_user"]`` to override the tenant-wide managed user.
    extra: dict[str, Any] = Field(default_factory=dict)


class IntendedConfig(BaseModel):
    device_id: str
    revision: str
    facts: DeviceFacts
    extra: dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime


class ObservedSnapshot(BaseModel):
    device_id: str
    facts: DeviceFacts
    raw: dict[str, str] = Field(default_factory=dict)
    collected_at: datetime


class SoTRevision(BaseModel):
    """Pointer to a write the executor performed back into the SoT.

    Kept for audit; not required for v1 flow."""

    device_id: str
    revision: str
    written_at: datetime


class Actor(BaseModel):
    """Identity of the caller of a ChangeProposal lifecycle method.

    The API constructs ``Actor(kind="user", user_id=...)`` from
    ``current_user``; the engine constructs ``Actor(kind="engine",
    name=...)`` from its run context; the executor service constructs
    ``Actor(kind="executor", scope=settings.executor_jwt_scope)``
    after verifying its dedicated JWT. The kind+scope combination is
    what change_proposals.execute() actually checks.
    """

    kind: Literal["user", "engine", "executor", "system"]
    user_id: uuid.UUID | None = None
    name: str | None = None
    scope: str | None = None
