# Adapter reference — what each adapter manages and how to extend

The adapter family is the boundary between daalu and a real device. Each
adapter implements the `DeviceAdapter` ABC and registers under a
transport string. The reconciler / executor look up the right adapter at
runtime via `get_device_adapter(transport)`.

```python
# core/device/base.py
class DeviceAdapter(ABC):
    transport: ClassVar[str]
    async def collect(creds, intended_hint=None) -> DeviceFacts: ...
    async def render(intended) -> RenderedConfig: ...
    async def diff(observed, intended) -> ConfigDiff: ...
    async def execute(creds, rendered) -> ExecutionResult: ...
```

## Five built-in adapters

| Transport | Module | Class | What it manages | Library |
| --- | --- | --- | --- | --- |
| `linux_ssh` | `core/device/linux_ssh.py` | `LinuxSSHAdapter` | hostname, authorized_keys, sysctl, packages, cloud-init | `asyncssh` |
| `redfish` | `core/device/redfish.py` | `RedfishAdapter` | BIOS attributes, boot override, power state | `httpx` (DMTF Redfish) |
| `junos` | `core/device/junos.py` | `JunosAdapter` | hostname, interfaces, VLANs, static routes | `scrapli-netconf` |
| `iosxr` | `core/device/iosxr.py` | `IOSXRAdapter` | same as Junos | `scrapli-netconf` |
| `eos` | `core/device/eos.py` | `EOSAdapter` | same as Junos | `scrapli` (CLI) |

Three universal rules:

1. **Manage only what intent specifies.** Never enumerate a device's full
   config and treat absence-in-intent as "delete." `sysctl` keys not in
   intent are left alone. BIOS attrs not in intent are left alone. Same
   for interfaces / VLANs / routes.
2. **Render must be deterministic.** Stale-detection compares
   `rendered.files` byte-for-byte; non-determinism creates ghost drifts.
   Sort dict keys; sort lists by a stable key; no timestamps in payloads.
3. **`execute()` is the only method `change_proposals.execute()` calls.**
   `collect` / `render` / `diff` are read-only and used freely by the
   reconciler and LLM tool.

## LinuxSSH adapter

**Transport:** `linux_ssh`
**Credentials provider:** `ssh_credentials`
**Default port:** 22

### Managed facts (`LinuxFacts`)

```python
class LinuxFacts(BaseModel):
    hostname: str | None = None
    authorized_keys: list[AuthorizedKey] = []         # per user
    sysctl: list[SysctlValue] = []                    # per kernel parameter
    packages: list[PackagePresence] = []              # apt / dnf
    cloud_init: CloudInitUserData | None = None       # NoCloud seed user-data
```

### Where each fact lands on disk

| Fact | Path |
| --- | --- |
| hostname | `/etc/hostname` (+ `hostnamectl set-hostname`) |
| authorized_keys | `~<user>/.ssh/authorized_keys` per user |
| sysctl | `/etc/sysctl.d/99-daalu.conf` (single file, sorted) |
| packages | `apt-get install` / `dnf install` / `apt-get remove` + sentinel file `/etc/daalu/packages.json` |
| cloud_init | `/var/lib/cloud/seed/nocloud/user-data` |

### Execute flow

1. `asyncssh.connect()` with the resolved Credentials
2. For each managed fact:
   - Read current contents (so we can snapshot for rollback)
   - Write new contents
3. Verify by re-collecting; if the verify reads back differently, restore from snapshot
4. Close connection
5. Return `ExecutionResult` with `per_step` trace

### What this adapter doesn't do

- No service management (systemd unit edits). v2 could add `services` to the schema.
- No user/group creation (only manages keys for users that exist).
- No file-content management beyond the narrow fact set above (don't try to render arbitrary `/etc/nginx/nginx.conf` here — that's a separate higher-level template engine).

## Redfish adapter

**Transport:** `redfish`
**Credentials provider:** `redfish_credentials`
**Default port:** 443 (HTTPS)

### Managed facts (`RedfishFacts`)

```python
class RedfishFacts(BaseModel):
    bios_attributes: list[BiosAttribute] = []   # name + value, both strings
    boot_override: BootOverride | None = None   # Pxe/Hdd/Cd/Usb/BiosSetup/UefiTarget/UefiHttp/UefiShell/Floppy/RemoteDrive/Utilities/Diags, Disabled/Once/Continuous
    power: PowerControl | None = None           # desired_state: "On" | "Off"
```

### Redfish endpoints used

| Read | Endpoint |
| --- | --- |
| Enumerate systems | `GET /redfish/v1/Systems/` |
| System info (power, boot) | `GET /redfish/v1/Systems/{id}` |
| BIOS attrs | `GET /redfish/v1/Systems/{id}/Bios` (404 → silently empty, some BMCs don't expose) |

| Write | Endpoint |
| --- | --- |
| BIOS PATCH (pending — applied on next boot) | `PATCH /redfish/v1/Systems/{id}/Bios/Settings` |
| Boot override | `PATCH /redfish/v1/Systems/{id}` body `{Boot: {BootSourceOverrideTarget, BootSourceOverrideEnabled}}` |
| Power | `POST /redfish/v1/Systems/{id}/Actions/ComputerSystem.Reset` body `{ResetType: "On" | "ForceOff"}` |

### Execute flow (BIOS → boot → power, in that order)

1. List systems, pick the first
2. If `bios_attributes` in intent: PATCH `/Bios/Settings`. Per-step trace records success.
3. If `boot_override` in intent: PATCH `/Systems/{id}` with the boot config
4. If `power` in intent: read current PowerState; if it differs, POST the Reset action. Skipped (with `per_step.skipped=true, reason="already in desired state"`) when state already matches — idempotent for re-runs.
5. Return `ExecutionResult` with the per-step trace

### TLS verification

BMCs typically ship with self-signed certs. `verify_tls=False` by default
in the credentials. Opt-in to verification via `verify_tls=true` in the
`redfish_credentials` config — that maps to `Credentials.known_hosts="verify"`
which the adapter checks before building the httpx client.

### What this adapter doesn't do

- **No firmware updates** via `UpdateService.SimpleUpdate`. Multi-step
  task polling, vendor-specific image format quirks — own PR.
- **No virtual media mount.**
- **No SEL log ingestion.**
- **No per-system enumeration beyond the first.** Single-BMC-many-systems
  setups are exotic and not supported in v1.

## Network adapters

The three network adapters share `NetworkFacts` and the
`network_credentials` provider; they differ in transport library + commit
semantics. Shared rendering / diff helpers live in
`core/device/_net_common.py` so per-adapter modules stay small.

### Common: `NetworkFacts`

```python
class InterfaceConfig(BaseModel):
    name: str                          # vendor-native name: "ge-0/0/0", "GigabitEthernet0/0/0/1", "Ethernet1"
    description: str | None = None
    enabled: bool = True
    mtu: int | None = None
    ipv4_address: str | None = None    # CIDR
    vlan_access: int | None = None     # for L2 access ports

class VlanDefinition(BaseModel):
    vlan_id: int
    name: str | None = None

class StaticRoute(BaseModel):
    prefix: str                        # CIDR
    next_hop: str
    vrf: str | None = None

class NetworkFacts(BaseModel):
    hostname: str | None = None
    interfaces: list[InterfaceConfig] = []
    vlans: list[VlanDefinition] = []
    static_routes: list[StaticRoute] = []
```

Out of scope for v1: BGP, OSPF, ACLs, route-maps, prefix-lists, QoS,
multicast, MPLS, VRF beyond the static-route `vrf` field. Each is its
own follow-up.

**IOS-XR VLAN scope:** IOS-XR has no first-class VLAN table the way
Junos / EOS do — VLANs live under L2VPN bridge-domains or sub-interface
encapsulation. Rather than fail proposals that include `vlans`, the
IOS-XR adapter records a `vlans.skipped` per-step note and proceeds with
hostname / interfaces / routes. If you need IOS-XR VLAN provisioning,
the schema extension lands in a follow-up PR.

**Junos VRF routes:** the Junos `collect()` path enumerates global-VRF
routes only; named-VRF routes (under `<routing-instances>`) are a wider
collect-side change and are out of scope for v1.

### Common: credentials

```python
class Credentials(BaseModel):
    user: str
    host: str
    port: int = 22              # NETCONF default 830 — adapter overrides internally
    private_key_pem: str | None = None
    password: str | None = None
    enable_password: str | None = None  # IOS-XR / EOS exec-mode escalation
    known_hosts: str | None = None
    sudo: bool = False                  # irrelevant for network gear
```

`enable_password` is for exec-mode escalation (Cisco's classic "enable"
privilege escalation). IOS-XR uses it on the device's exec-mode prompt;
EOS picks it up via scrapli's `auth_secondary` parameter. Junos has no
enable mode — the Junos adapter ignores it.

### Junos adapter (`junos`)

- Transport: NETCONF/SSH on port 830 via `scrapli-netconf`
- Commit semantics: `commit confirmed 600` followed by a `commit` to
  confirm. If our connection dies before the second commit, the change
  auto-rolls back after 10 minutes (configurable via
  `commit_confirmed_timeout_s`)
- Vendor: Juniper SRX, MX, QFX, EX

### IOS-XR adapter (`iosxr`)

- Transport: NETCONF/SSH via `scrapli-netconf`
- Commit semantics: same `commit confirmed <timeout>` pattern (IOS-XR
  supports it natively)
- Vendor: Cisco ASR9K, NCS, CRS

### EOS adapter (`eos`)

- Transport: SSH CLI via `scrapli` (not eAPI — Scrapli is the more
  common automation surface, scripts/runbooks transfer cleaner)
- Commit semantics: **no native commit-confirmed.** v1 uses `write
  memory` to persist + relies on operator approval being the gate. The
  EOS adapter's module docstring captures the tradeoff: a malicious
  push that locks the operator out is conceivable but mitigated by
  scrapli's per-command timeout (the session drops and the change-but-
  no-save means the device reverts on next reboot)
- Vendor: Arista EOS-running gear

### Rendered file layout (all three)

```
/_net/hostname.json
/_net/interfaces/<name>.json   # one file per managed interface
/_net/vlans.json
/_net/static_routes.json
```

Per-interface granularity means stale-detection only re-fires for the
specific interface that changed — not a whole bundle.

## Adding a new adapter (cookbook)

Say you want to add FortiOS (Fortinet firewalls). Steps:

1. **Pick the transport story** — CLI via Scrapli? REST API? FortiOS has
   both; the REST API is well-documented and idempotent. Pick REST.

2. **Add facts schema** in `core/sot/models.py`:
   ```python
   class FortiPolicy(BaseModel):
       name: str
       srcintf: list[str]
       dstintf: list[str]
       srcaddr: list[str]
       dstaddr: list[str]
       service: list[str]
       action: Literal["accept", "deny"]

   class FortiOSFacts(BaseModel):
       hostname: str | None = None
       policies: list[FortiPolicy] = []
   ```
   Add `FortiOSFacts` to the `DeviceFacts` union.

3. **Export from `core/sot/__init__.py`** — add to `__all__`.

4. **Extend NautobotSoT dispatch** in `core/sot/nautobot.py`:
   - `_parse_fortios_facts(blob) -> FortiOSFacts`
   - `_fortios_facts_to_blob(facts) -> dict`
   - Add branches in `_dispatch_parse` and `_dispatch_to_blob`
   - Add to `_platform_bucket` (recognize `fortios`, `fortigate` platform names)
   - Add to `_to_device`'s transport fallback (`bucket == "fortios" → transport = "fortios"`)

5. **Create `core/device/fortios.py`:**
   - `class FortiOSAdapter(DeviceAdapter)` with `transport: ClassVar[str] = "fortios"`
   - `_build_http_client(creds)` module-level factory for test injection
   - `_render(facts)` deterministic file mapping
   - `_diff(observed, intended)` per-fact + unified diff
   - `collect / render / diff / execute` methods
   - `register_device_adapter(FortiOSAdapter)` at the bottom

6. **Side-effect import** in `core/device/__init__.py`:
   ```python
   from daalu_automation.core.device import fortios  # noqa: F401
   ```

7. **Credentials wiring** in `core/change_proposals.py`:
   - Add `"fortios": "fortios_credentials"` to `_CREDS_PROVIDER_BY_TRANSPORT`
   - Add a branch in `resolve_credentials` for the FortiOS shape

8. **Integrations API** (`api/routers/integrations.py`):
   - Add `"fortios_credentials"` to `_REDACT_FIELDS`
   - Add `"fortios_credentials": ("infra", "FortiOS credentials")` to `_ADAPTERLESS`

9. **Tests** in `tests/test_fortios_adapter.py` — mirror the
   `test_redfish_adapter.py` layout (render / diff / collect / execute /
   registry / NautobotSoT dispatch / resolve_credentials).

10. **Documentation** — add the new transport row to this file's table.

Time estimate: about a day of focused work for a moderately well-documented
device API (FortiOS REST is well-documented), 2–3 days for a CLI-only
device where you have to scrape configs.

## Common adapter problems

### `TypeError: expected RedfishFacts, got LinuxFacts`

The dispatcher passed the wrong facts type to the adapter. Common cause:
the device's transport in Nautobot doesn't match the adapter's `transport`
ClassVar. Check `custom_field_data.daalu_transport` or the platform name.

### Renderer mismatch causing perpetual "stale"

The same input produces different rendered output on different runs.
Common cause: a dict iteration where keys aren't sorted, or a timestamp
embedded in the rendered output. Render must be deterministic — fix the
adapter's `_render`.

### `collect()` returns a different shape than expected by `diff()`

`collect()` is allowed to return a sparse `DeviceFacts` (only the keys it
actually observed). `diff()` should treat missing keys in observed as "no
value" and compare against intent. If the diff is firing because observed
is empty for a fact that exists in intent, the diff is correct (the fact
doesn't exist on the device) — that's a real drift, not a bug.

### `execute()` succeeds but the next reconciler pass shows drift

See the troubleshooting note in [`setup-guide.md`](setup-guide.md). Common
causes: silent device rejection (BIOS PATCH on a BMC that requires reboot,
NETCONF commit that landed but the config doesn't apply for some vendor-
specific reason), race with the reconciler reading stale state.
