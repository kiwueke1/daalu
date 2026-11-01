# 98. Cable validation and zero-touch provisioning (ZTP)

Two NV-CM capabilities that sit at either end of a switch's life in a fabric:
**ZTP** takes a blank switch to a configured one with no console cable, and
**cable validation** proves the physical wiring matches the plan once it's
racked. Both are deterministic automation, no model involved. This chapter
covers how each works upstream, how Daalu installs and reaches them, and
what's still missing for each on a Daalu-deployed environment. It also places
cable validation next to Daalu's own LLDP topology view (chapter 79), which
answers a related but different question.

> **Upstream vs Daalu.** The cable-validation and LLDP workflows, the ZTP
> service, the Kea DHCP service and the boot-script templates are **upstream
> NV-CM, outside Daalu's code**. Daalu toggles the `ztp` and `dhcp`
> components, runs the stack, and (for cable checks) gets you to the workflow
> UI and the MCP server. The in-cluster ZTP server used in the lab is a Daalu
> lab artefact, not NV-CM. Upstream references (`nv-config-manager/<path>:<line>`)
> are pinned to tag **`1.3.0` (`7455da6`)**, the commit Daalu vendors; where
> upstream HEAD (`2dad101`) differs, the text says so.

| Piece | Owner | Path |
|---|---|---|
| Site / Device cable validation workflows | upstream | `nv-config-manager/src/nv_config_manager/temporal/ngc/workflows/cable_validation.py` |
| Row-building (intended vs actual) | upstream | `nv-config-manager/src/nv_config_manager/temporal/ngc/activities/cable_validation.py` |
| Port LLDP Info workflow | upstream | `nv-config-manager/src/nv_config_manager/temporal/ngc/workflows/lldp.py` |
| ZTP service (`/v1/device/...`) | upstream | `nv-config-manager/src/nv_config_manager/ztp/api/device_v1.py` |
| Kea DHCP config generation | upstream | `nv-config-manager/src/nv_config_manager/dhcp/` |
| Cumulus boot-script template | upstream | `nv-config-manager/components/network-templates/src/nv_config_manager_templates/templates/cumulus-linux/role_common/base/boot-script.j2` |
| `ztp` / `dhcp` component toggles (off by default) | Daalu | `src/daalu_automation/config_manager_controller/values.py:89-108`, `deployer_config.py:51-58` |
| No device-facing LoadBalancer | Daalu | `src/daalu_automation/config_manager_controller/deployer_config.py:191-196` |
| Daalu's own LLDP sweep + topology | Daalu | `src/daalu_automation/core/topology/lldp.py` (chapter 79) |
| Lab ZTP stand-in (dnsmasq + httpd, Multus leg) | Daalu lab | `docs/content/lab/ztp/` |

---

## Part A — Cable validation

## 98.1 The idea: the cabling plan is the answer key

The cables you modelled in Nautobot to *build* the fabric are also the oracle
for *checking* it. Every switch already knows its real neighbours through
LLDP. Compare the two per port and a crossed cable becomes a row: start port,
intended far end, actual far end, issue. A crossed pair always appears twice
(once on each switch), which is how you tell a swap from a missing cable.

## 98.2 Three workflows, three scopes

| Workflow | Endpoint | Scope | Input highlights |
|---|---|---|---|
| **Site Cable Validation** | `/ngc/site_cable_validation` | every matching device at a site | `site`, `roles`, `status`, `tenant`, `device_type_ids`, `raise_for_invalid` (`cable_validation.py:116-137`) |
| **Device Cable Validation** | `/ngc/device_cable_validation` | one switch | `device_id`, `ignore_no_neighbor` (`cable_validation.py:140-157`) |
| **Port LLDP Info** | `/ngc/port_lldp_info` | one port, or locate a remote MAC | `device_id`, `interface`, `remote_mac_address` (`lldp.py:51-60`) |

All three are read-only towards the device and **no stage requires
approval** (`cable_validation.py:190-206`, `:489-522`). All three are flagged
`workflow_mcp_enabled = True` (`cable_validation.py:185,484`, `lldp.py:73`),
so they're also MCP tools (chapter 99).

### Device Cable Validation, stage by stage

`DeviceCableValidationWorkflow` (`cable_validation.py:473-522`):

1. `get_device_data` — device record from Nautobot.
2. `validate_device_hostname` — the device answers as who Nautobot says it is.
3. `get_device_intended_neighbors` — the cables in Nautobot
   (`cable_validation.py:541-558`).
4. `get_device_actual_neighbors` — LLDP neighbours read from the device
   (`cable_validation.py:622-639`).
5. `get_device_mac_table` — MAC and ARP tables, used when LLDP isn't enough.
6. `validate_connections` — the comparison.

Each mismatch becomes a row with an `Issue`
(`activities/cable_validation.py:516-640`):

| Issue text | Meaning |
|---|---|
| `Incorrect cabling, actual should match intended. Based on LLDP data` | LLDP names a different far end than the plan (`:612`) |
| `Incorrect cabling, actual should match intended. Based on expected MAC …*` | Matched by MAC instead of LLDP name (`:580-582`); still a real finding |
| `Link is down.` | Port down (`:556`) |
| `Link is up but no neighbor found` | Up, but LLDP hasn't (re)learned yet (`:55`, `:635`) |
| `Unexpected connection found` | A neighbour that isn't in the plan (`:533`) |

MAC matching tolerates the common `+0x10` offset between a host's
interface MAC and the MAC it advertises
(`activities/cable_validation.py:150-178`). The workflow also produces a CSV
export; the markdown table drops troubleshooting columns
(`activities/cable_validation.py:48-52`).

### Site Cable Validation fans out

The site workflow selects devices from Nautobot by the input filters plus a
platform allow-list of Cumulus Linux, Arista EOS and NV-OS
(`cable_validation.py:110`, `:223-252`), then starts **one Device Cable
Validation child workflow per device** (`cable_validation.py:286-292`). A
site run's rows are the union of its children's rows.

### What the creator should verify: the default filters changed after 1.3.0

At the 1.3.0 tag the site workflow's defaults are NVIDIA-internal values —
roles `tan-*`/`cin-*`/`smn-*`, status `active`/`provisioning`, tenant `nsv`
(`cable_validation.py:88-102`). A lab fabric with roles like `leaf` and no
tenant would match **nothing** with those defaults and report "No devices
found matching the specified filters" (`cable_validation.py:241-246`).
Upstream HEAD changed them to roles `[]`, status `Active`/`Provisioned`,
tenant `None`. Which defaults you get depends on the source the images were
built from: Daalu's `deploy/scripts/config-manager/build-push-images.sh`
builds whatever `SRC_REPO` has checked out and tags it `1.3.0`
(`build-push-images.sh:34-35,52-57`). If the "default status" run matches no
devices, pass `roles`, `status` and `tenant` explicitly.

## 98.3 How this relates to Daalu's topology view

Daalu has its own LLDP pipeline, independent of NV-CM:
`core/topology/lldp.py:collect_observations` SSHes every device, reads
`lldpctl -f keyvalue`, and the Topology page overlays observed links on
intended cables (chapter 79 §79.1–79.2). The two answer different questions:

| | Daalu topology (ch. 79) | NV-CM cable validation |
|---|---|---|
| Output | a live graph, links coloured | a table of per-port findings |
| Runs | continuously, on page poll (TTL-cached) | on demand, as a Temporal workflow with a run record |
| Evidence | LLDP only | LLDP, then MAC/ARP fallback |
| Devices | SSH-shaped transports; `config_manager`-transport devices are skipped (`lldp.py:166-185`) | anything NV-CM manages on a supported platform |
| Audit trail | none | workflow run, stages, user, link |

Daalu code does not call the cable-validation workflows: the only workflow
the hub starts is `DeployWorkflow`
(`core/configmgr/client.py:223-232`). You run cable checks from the NV-CM
workflow UI (reached through the hub tool proxy as the bare-host Config
browser, `api/tool_proxy.py:97-103,195-196`) or through NV-CM's MCP server.

---

## Part B — Zero-touch provisioning

## 98.4 The handshake

A switch with factory config:

1. DHCPs on its management network;
2. receives an address **and a boot-script URL** — DHCP option 239
   (`cumulus-provision-url`) on Cumulus (`docs/content/lab/ztp/dhcpd.conf:9`);
3. fetches the script and runs it only if it contains the
   `CUMULUS-AUTOPROVISIONING` marker;
4. the script configures the switch.

DHCP broadcasts don't cross routers, so the DHCP server must be L2-adjacent
to the management segment or sit behind a relay.

## 98.5 How NV-CM does it

- **Kea DHCP**, configured from Nautobot: reservations come from IPs assigned
  to device interfaces, with options from Nautobot data. A device whose
  Config Manager Device record has `ztp_enabled` off is skipped with
  `Reserved IP … is not ZTP enabled, skipping`
  (`dhcp/nautobot.py:86-98`, `:196-205`). Note the asymmetry: a device with
  **no** Config Manager Device record passes this filter
  (`.get("ztp_enabled", True)`), even though the model's default for a new
  record is `False` (`models.py:66`).
- **The boot script is a rendered file.** `boot-script` is just another
  render entry point (chapter 96 §96.1). The ZTP service serves
  `GET /v1/device/{uuid}/boot-script` and `GET /v1/device/{uuid}/config/{file}`
  straight from the Config Store's intended files
  (`ztp/api/device_v1.py:79-100`, `ztp/device.py:96-104`).
- **The Cumulus boot script** (`boot-script.j2`) upgrades firmware via ONIE if
  the running version isn't the intended one (`:47-50`), posts the switch's
  serial to `validate_serial` (`:90-95`), downloads `startup.yaml` (`:102`),
  applies it, calls `provisioned` (`:123`), and carries the
  `CUMULUS-AUTOPROVISIONING` marker (`:131`).
- **Who may fetch it.** `_authorize_request` (`ztp/api/device_v1.py:35-76`):
  an authenticated SSO/JWT/mTLS caller passes; otherwise the client IP must
  be one of the device's IPs in Nautobot (plus `127.0.0.1`), else **403**.
- **Serial check.** `validate_serial` compares the reported serial with
  Nautobot's, accepting a suffix match either way because some devices
  report part number + serial (`device_v1.py:180-215`). Mismatch → 400 and
  the script stops.
- **Completion.** `provisioned` sets the Nautobot device status to
  `Provisioned` and starts a backup workflow (`device_v1.py:157-171`;
  chapter 97 §97.3), so the first backup version exists the moment ZTP ends.

## 98.6 What a Daalu-deployed environment has today

- **ZTP and DHCP are off by default.** The "Lean" profile is
  `render + configStore + temporal + nautobot`
  (`config_manager_controller/values.py:100-108`). The `ztp` and `dhcp` keys
  map to the chart's `networkZtp` / `networkDhcp`
  (`values.py:94-95`) and the Deployer's services (`deployer_config.py:56-57`).
  Turning on `dhcp` also turns on NV-CM's MCP server (chapter 99 §99.2).
- **No device-facing path.** The chart exposes ZTP (HTTP/HTTPS/SFTP) and DHCP
  (UDP 67) to devices through a separate LoadBalancer
  (`deploy/charts/nv-config-manager-1.3.0/values.yaml:1294-1299`,
  `:1465-1470`). Daalu's install config leaves LoadBalancer at `NONE`
  because the shared Envoy Gateway is fronted by traefik
  (`deployer_config.py:191-196`). Nothing in Daalu gives the Kea pod an L2
  leg on a switch management segment. So with Daalu's defaults the ZTP and
  DHCP services run, but a blank switch on the OOB network cannot reach them.
- **Daalu creates no Config Manager Device records** (chapter 96 §96.4), so
  `ztp_enabled` and the reservations it controls are set in Nautobot by hand.

### The lab stand-in

To demonstrate the handshake end to end, the lab runs its own server inside
the cluster: `docs/content/lab/ztp/k8s-ztp-dhcp.yaml` (dnsmasq for DHCP with
`--dhcp-option-force=239,http://172.20.20.240/ztp.sh`, busybox httpd for the
script) with a **Multus bridge-CNI secondary interface** attached straight to
the containerlab management bridge — the L2 leg production would also need.
The script is hand-written (`docs/content/lab/ztp/ztp.sh`), not rendered from
Nautobot, and there is no serial check or `provisioned` callback. Rationale
and the one-shot validation are in `docs/runbooks/gpu-node-1-standalone-rebuild.md`
Phase 5b and `docs/content/lab/ztp/README.md`.

What the creator should verify:

- The NAD's `bridge` matches the **current** `clab-mgmt` bridge name; a
  containerlab redeploy mints a new one (`k8s-ztp-dhcp.yaml:25-30`).
- `networkop/cx` disables the ZTP service, so trigger it explicitly with
  `sudo ztp -v -r <url>`; say so on camera.
- Don't claim on camera that the script was rendered from Nautobot, that a
  serial check ran, or that a status flipped: none of that happens in the lab
  path.

---

## 98.7 Failure modes

| Symptom | Cause | Where |
|---|---|---|
| Site validation: "No devices found matching the specified filters" | 1.3.0's NVIDIA-internal default roles/tenant | `cable_validation.py:88-102,241-246` |
| Row `Link is up but no neighbor found` | LLDP not re-learned after a recable; wait ~30 s and re-run | `activities/cable_validation.py:55` |
| Row `Link is down.` | Port admin/oper down | `activities/cable_validation.py:556` |
| ZTP fetch → 403 | Client IP isn't one of the device's IPs in Nautobot | `ztp/api/device_v1.py:63-76` |
| ZTP `validate_serial` → 400 | Serial in Nautobot doesn't match the box | `device_v1.py:202-212` |
| Kea logs `… is not ZTP enabled, skipping` | `ztp_enabled` off on the device's record | `dhcp/nautobot.py:196-205` |
| Lab: no DHCP offer | Multus leg on a stale bridge | `docs/content/lab/ztp/k8s-ztp-dhcp.yaml:25-30` |

---

## Related chapters

- [Chapter 79 — Live fabric topology & the device terminal](79-network-topology-and-device-terminal.md): Daalu's own LLDP view.
- [Chapter 96 — Config rendering and the render gate](96-config-rendering-and-render-gate.md): how `boot-script` and `startup.yaml` are produced.
- [Chapter 97 — Config backups and diffs](97-config-backups-and-diffs.md): the backup ZTP triggers.
- [Chapter 99 — NV-CM's MCP server and its workflow allow-list](99-nvcm-mcp-server-and-workflow-allow-list.md): the cable and LLDP checks as agent tools.
- [Chapter 23 — NV-CM internals](23-nvcm-internals-and-operator.md) §23.1: the ZTP and DHCP rows.
- [Chapter 20 — Config-management planes](20-config-management-planes.md): who owns what.
