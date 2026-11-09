# 96. Config rendering, the template tree, and the `render_enabled` gate

Chapter 23 lists NV-CM's **Render Service** as one row in a table. This chapter
is the deep dive: how a switch's config is generated from the source of truth,
how the template tree is selected, what makes a render deterministic, which
devices are allowed to render (the `render_enabled` gate), and — just as
important — which of those pieces Daalu owns and which it only installs.

> **Upstream vs Daalu.** The renderer, the templates, the Config Store and the
> `render_enabled` flag are **upstream NV-CM (NVIDIA Config Manager), outside
> Daalu's code.** Daalu vendors the chart and installer (chapter 23 §23.3),
> builds the images from the upstream source, wires the gateway and identity,
> and holds a client for the render API. Upstream references below use the
> form `nv-config-manager/<path>:<line>` and are pinned to the tag Daalu
> vendors — **`1.3.0`, commit `7455da6`** (`components/VENDORED.md`). Read
> them in a checkout of `NVIDIA/nv-config-manager` at that tag. Daalu
> references are repo-relative as everywhere else in this manual.

| Piece | Owner | Path |
|---|---|---|
| Render API (`/v1/render/...`) | upstream | `nv-config-manager/src/nv_config_manager/render/api/render_v1.py` |
| Render engine entry point | upstream | `nv-config-manager/src/nv_config_manager/render/render.py:104` (`execute_render`) |
| Change-driven render queue + gate | upstream | `nv-config-manager/src/nv_config_manager/render/events/util.py:119` (`should_run`) |
| Template engine + reference templates | upstream | `nv-config-manager/components/network-templates/` (`nv-config-manager-templates` package) |
| `render_enabled` flag (Nautobot plugin model) | upstream | `nv-config-manager/components/nautobot/nautobot-nv-config-manager/nv_config_manager/models.py:65` |
| Versioned store + diff | upstream | `nv-config-manager/src/nv_config_manager/config_store/` |
| Render component toggle (on by default) | Daalu | `src/daalu_automation/config_manager_controller/values.py:89-108` |
| Render URLs (`svc-render`, `render-<slug>`) | Daalu | `src/daalu_automation/config_manager_controller/values.py:149,156` |
| Hub client for the render API | Daalu | `src/daalu_automation/core/configmgr/client.py:199-214` (`RenderClient`) |
| Hub browser access ("Consoles → Rendering") | Daalu | `src/daalu_automation/api/tool_proxy.py:97-103` (`render` tool prefix) |
| Template-plugin PVC loader pinning | Daalu | `src/daalu_automation/config_manager_controller/deployer_runner.py:305-310` |

---

## 96.1 The model: config is an output

The premise NV-CM is built on is that a switch config is a **function of the
source of truth**. Nautobot holds devices, interfaces, cables, IP addresses,
config contexts; the Render Service turns that data into vendor-native files
and stores each result as a new version in the Config Store. Nobody edits a
rendered file; you edit data and re-render.

The engine is deliberately small. Its README states the whole model
(`nv-config-manager/components/network-templates/README.md:11-39`):

1. Load the device's data from Nautobot with one core GraphQL query.
2. Resolve the device's site (or a plugin-provided location name).
3. Load the location's data with a second core query.
4. Run any plugin GraphQL queries, keyed by query name.
5. Select every template under
   `<normalized-platform>/<normalized-role>/<desired-firmware>/entrypoint/`.
6. Render each entrypoint with three context variables: `device_data`,
   `location_data`, `plugin_data`.

"Normalized" means lower-cased with whitespace turned into hyphens, so
platform `Cumulus Linux` + role `Leaf-Common` + firmware `5.13.1` selects
`cumulus-linux/leaf-common/5.13.1/entrypoint/`. The firmware component is the
`intended-firmware.version` key of the device's config context (the ZTP service
reads the same key — `nv-config-manager/src/nv_config_manager/ztp/device.py:119-121`).

Each `*.j2` file in that `entrypoint/` folder produces **one output file**,
named after the template minus `.j2` (`render.py:129` strips the path and
extension). Typical outputs are `startup.yaml` (Cumulus NVUE), `full-config`,
and `boot-script` (the ZTP script — chapter 98).

### The template tree

`nv-config-manager/components/network-templates/README.md:41-63`:

```text
<platform>/
  <role>/
    base/                      # reusable skeletons with Jinja blocks
    include/                   # shared partials
    <firmware-version>/
      entrypoint/              # one rendered file per template here
      include/                 # only what truly differs for this firmware
```

At 1.3.0 the reference tree ships `cumulus-linux/` (including `leaf-common`,
`spine-common`, `role_common`, `storage-leaf`, `border-leaf`, …), `nv-os/` and
`mlnx-os/`. There is **no Arista tree**, even though the backup scheduler lists
Arista EOS as a supported platform (chapter 97).

### Design rules the README enforces (and why)

- **Inheritance over copying.** Entry points `{% extends %}` a base and
  override the smallest block. A copied `startup.yaml.j2` per role fans every
  firmware fix out across N files.
- **No GraphQL in templates** (`README.md:130-155`). Templates call filter
  wrappers (`interfaces`, `asn`, `site_name`, …), never raw response paths, so
  a Nautobot schema change is fixed in one Python wrapper instead of every
  template. The rejected alternative — reaching into `device_data.data.device.…`
  directly — is shown in the README as the pattern not to add.
- **Data logic in Python, formatting in Jinja.** Filters sort deterministically
  and raise `FilterException` on missing or inconsistent data. That is what
  makes a render both *repeatable* and *loud*: missing required data fails
  the render instead of producing a quietly wrong file.

---

## 96.2 One render, step by step

`execute_render(device_uuid, commit_message, user)`
(`nv-config-manager/src/nv_config_manager/render/render.py:104-195`):

1. Build a `Renderer` against Nautobot (`render.py:113-116`) and call
   `render_entrypoints(device_id=…)` in a worker thread (`render.py:120`).
2. **No matching templates → empty result, no error** (`render.py:121-126`).
   A device whose platform/role/firmware path does not exist renders
   nothing and logs `No entrypoint templates matched`. This is the single
   most confusing behaviour in the system: a role is a *template selector*,
   and a typo in it, or a missing `intended-firmware` context, is silent.
3. Persist all output files to the Config Store in one call
   (`render.py:136-138`).
4. If a **deployable** file changed — `DEPLOYABLE_FILES = ["startup.yaml",
   "full-config"]` (`render.py:41`) — record the new commit on the
   device's `IntendedConfig` row in the Nautobot plugin, stamped with the
   template version (`render.py:150-159`, `_update_nautobot_plugin` at
   `render.py:48-89`). Otherwise just bump the template version.
5. If **nothing** changed, re-sync Nautobot with the latest stored version
   (so a re-added device gets its intended-config record back) and return an
   empty list (`render.py:164-195`).

### Why "same data, same bytes" holds

The Config Store only writes a new version when content differs from the
latest version of that file:
`nv-config-manager/src/nv_config_manager/config_store/core/storage.py:216-223`
(`create_or_update_config`) compares the decompressed latest content with the
incoming content and returns the existing row on a match. A SHA-256 content
hash is stored alongside each version (`storage.py:228`). Combined with
deterministic filters (§96.1), re-rendering unchanged data stores nothing and
the API answers **200 with `updated_files: []`**; a real change answers
**201** with the new commits (`render_v1.py:134`). That is the property that
makes a two-line diff between two versions meaningful.

### Template upgrades re-render on their own

Every render records the template package's version key. A separate producer
finds devices whose last render used an older template version and re-renders
them under the per-device lock
(`nv-config-manager/src/nv_config_manager/render/producer.py:46-61` and
`:96-121`). Upgrading the templates is therefore itself a data change that
fans out to every affected device. Note its query has no `render_enabled`
filter (`producer.py:31-43`, with an upstream TODO); it only selects devices
that already have an intended config.

---

## 96.3 The three ways a render starts

| Trigger | Endpoint / path | Gate applied? |
|---|---|---|
| One device, on demand | `POST /v1/render/{device_uuid}/render` (`render_v1.py:115-137`) | **No** — calls `execute_render` directly |
| Every enabled device | `POST /v1/render/all` (`render_v1.py:140-176`) | **Yes** — selects `render_enabled: true` via GraphQL (`render_v1.py:88-112`) |
| A list of devices | `POST /v1/render/batch` (`render_v1.py:179-217`) | **Yes** — per device, at enqueue time |
| A Nautobot change | NATS event → `EventDispatcher` → `queue_render` | **Yes** — at enqueue time |

The single-device endpoint takes a Redis lock named after the device with a
30 s acquire timeout and returns **409 `Could not acquire lock within
timeout`** if a render is already running (`render_v1.py:129-131`; lock
implementation `render/lock.py:80-103`, default hold timeout 180 s). Retry;
don't "fix" it.

`render/all` and `render/batch` do not render inline: they publish one NATS
message per device and return **202** with a count. The pull consumer renders
them asynchronously (`render/dispatch.py:166-175`,
`nautobot_change_dispatch`).

### Change-driven rendering

Nautobot publishes change events onto NATS. The dispatcher maps the event's
model to a handler by module and function name
(`render/dispatch.py:109-127`): `dcim.device`, `dcim.interface`, `dcim.cable`,
`ipam.ipaddress`, `extras.configcontext`, the BGP models, and the plugin's own
`configmanagerdevicestatus` (`render/events/__init__.py`). Each handler works
out which managed devices the changed object touches and calls `queue_render`
(`render/events/util.py:231`). Deletes of the device itself are ignored
(`render/events/dcim.py:29-33`).

`queue_render` de-duplicates: if a device already has a pending render, the
new request is dropped (`events/util.py:186-193`). An edit storm on one
switch therefore produces one render, not fifty.

**What the creator should verify:** the delay between saving an edit in
Nautobot and a new version appearing. It depends on Nautobot's event
publishing, NATS and the consumer being healthy in the environment; nothing
in Daalu shortens or monitors it. If it never fires, call the single-device
endpoint and say so.

---

## 96.4 The `render_enabled` gate

Each device NV-CM manages has a **Config Manager Device** record in the
Nautobot plugin
(`components/nautobot/nautobot-nv-config-manager/nv_config_manager/models.py:57-69`,
`ConfigManagerDeviceStatus`) with four independent opt-ins, **all defaulting
to `False`**:

| Flag | Gates |
|---|---|
| `render_enabled` | change-driven, `all` and `batch` renders (this chapter) |
| `deploy_enabled` | deploy workflows |
| `backup_enabled` | scheduled backups (chapter 97) |
| `ztp_enabled` | DHCP reservations for ZTP (chapter 98) |

plus `is_aggregate_managed`, which must match the environment's
`aggregate.is_aggregate_environment` setting.

`should_run(device_uuid)` (`render/events/util.py:119-136`) is the gate:

- no Config Manager Device record → `False`;
- `render_enabled` false → `False`;
- otherwise `True` only if the device's `is_aggregate_managed` equals the
  environment's.

A gated-out device makes `queue_render` raise `DeviceNotEnabledError`
(`events/util.py:265-269`), which the dispatcher counts as **skipped**, not
failed (`render/dispatch.py:152-154`). In `render/batch` responses it appears
under `failed_devices` with `"<uuid> is not enabled for configuration renders."`

**What default-off buys you.** Rendering writes the intended config a later
deploy pushes. With a per-device opt-in, importing a site into Nautobot does
not produce deployable configs for every switch in it; you stage a rollout by
flipping flags. Our own lab assessment called it out as "an explicit opt-in
list. Good primitive for staged rollout"
(`docs/content/lab/config-automation/backbone-config-automation.md:1360`).

**The asymmetry to remember:** the single-device `POST
/v1/render/{uuid}/render` does **not** call `should_run`
(`render_v1.py:115-137`). It renders any device the caller names, enabled or
not. `render/all` is the call that proves the gate.

**Where the flag gets set.** In the Nautobot UI ("Config Manager Devices") or
the plugin REST API at `/api/plugins/nv-config-manager/configmanagerdevicestatus/`
(`api/urls.py:22`, `__init__.py:40` `base_url`). **Daalu never creates these
records**: nothing under `src/` writes `configmanagerdevicestatus`, and Daalu's
own SoT pipeline (chapters 14–15) writes Config Contexts, not plugin rows. Turn
on `render_enabled` for exactly the switches you want rendered (not the
servers) before calling `render/all`.

---

## 96.5 What Daalu owns

### Installing it

`render` is part of the default "Lean" component set
(`config_manager_controller/values.py:100-108`), toggled onto the chart's
`renderService` (`values.py:89-97`) and onto the Deployer's `render` service
(`deployer_config.py:51-58`). The chart's template-plugin PVC
(`render-service-template-plugins`) is populated by the Deployer; Daalu's
subclass pins the loader pod to the node already holding the RWO volume
(`deployer_runner.py:305-310`, background in chapter 48 §48.10.4). Daalu
ships **no templates of its own** — the reference tree inside the image is
the upstream package.

### Reaching it

- **Machine:** `render_url = http://svc-render.<base_hostname>`
  (`values.py:149`), copied into `Integration(provider="config_manager")` and
  read by `conn_from_integration_config` (`core/configmgr/client.py:66-85`).
- **Browser:** `render_human = https://render-<slug>.<zone>/docs`
  (`values.py:156`). The render service is API-only — `/` is a FastAPI 404 —
  so the console link lands on Swagger. That Swagger page is what the UI
  calls **Consoles → Rendering**. It is reached through the hub tool proxy,
  which recognises the `render` host prefix (`api/tool_proxy.py:97-103`) and
  injects a per-user (or service-fallback) Keycloak bearer
  (`tool_proxy.py:350-380`; chapter 68 §68.5). A stale hub login yields 401s
  on the tool console; log in again.

### Calling it — and what the change-proposal path actually does

`RenderClient.render_device` (`core/configmgr/client.py:205-214`) wraps
`POST /v1/render/{uuid}/render`. **Nothing in `src/` calls it today.** The
design doc listed the render call as an optional step of the deploy sequence
(`docs/design/nv-config-manager-integration.md` §9.2), and the code took the
other branch: the NV-CM executor
(`core/device/config_manager.py:108-119`) serialises the approved
`NetworkFacts` to Daalu's own deterministic canonical text
(`_canonical_network_text`, `config_manager.py:58-84`) and stages *that* as
`startup.yaml` (`DEFAULT_FILENAME`, `config_manager.py:50`) before starting
`DeployWorkflow` (`config_manager.py:153-165`; chapter 21 §21.3).

So there are two writers of a device's `startup.yaml` in the Config Store —
NV-CM's Render Service (Jinja, from Nautobot) and Daalu's executor (canonical
text, from an approved change proposal) — and no code reconciles them. Treat
the render pipeline in this chapter and the change-proposal executor as
separate paths. Before relying on the executor for an NV-CM-managed Cumulus
device, check which version a deploy would push.

---

## 96.6 Reading the result: versions and diffs

The Config Store keeps every version per `(device, filename, file_type)`;
`file_type` is `intended` (renders) or `backup` (chapter 97)
(`config_store/db/models.py:29-30`).

- `GET /v1/config/{device}/{file}/versions` (`config_store/api/config_v1.py:260`)
- `GET /v1/config/{device}/{file}/diff?from_version=&to_version=&file_type=`
  (`config_v1.py:302-336`) — a server-side `difflib.unified_diff` between two
  versions **of the same file type**.

Daalu's `ConfigStoreClient` wraps both (`core/configmgr/client.py:170-196`).
The "Config browser" console renders the same API: the history page diffs two
selected versions in a **Side-by-Side Comparison** card
(`nv-config-manager/ui/src/app/device/[uuid]/[filename]/history/page.tsx:182-183,316`).

Be precise on camera: this is **version vs version in the store**, not intended
vs the running switch. The running-vs-intended comparison happens in the
backup workflow's `check_drift` stage (chapter 97 §97.3) and in the deploy
workflow's `perform_configuration_diff` stage.

---

## 96.7 Failure modes

| Symptom | Cause | Where |
|---|---|---|
| `render/all` → `Queued renders for 0 devices` / `No render-enabled devices found` | No Config Manager Device rows with `render_enabled` | `render_v1.py:157-161` |
| 200 with no files, nothing in the store | No template folder matched platform/role/firmware | `render.py:121-126` |
| Render fails with a `FilterException` | Required data missing (e.g. no IPv4 on `lo`, missing secret mapping) | filters in `nv_config_manager_templates/filters/` |
| 409 `Could not acquire lock within timeout` | A render for that device is in flight | `render_v1.py:129-131` |
| Nautobot edit, no new version | Device not enabled, event pipeline not delivering, or rendered output unchanged | `events/util.py:119-136`, `storage.py:216-223` |
| Tool console 401 | Stale hub session / token exchange failed | chapter 68 §68.5 |

---

## Related chapters

- [Chapter 20 — Config-management planes](20-config-management-planes.md): why NV-CM owns rendering for switches.
- [Chapter 21 — The NV-CM network plane](21-nvcm-network-plane.md): the executor and the event bridge.
- [Chapter 23 — NV-CM internals & the controller](23-nvcm-internals-and-operator.md): services, install, Deployer.
- [Chapter 97 — Config backups and diffs](97-config-backups-and-diffs.md): the `backup` file type and drift.
- [Chapter 98 — Cable validation and ZTP](98-cable-validation-and-ztp.md): the `boot-script` output in use.
- [Chapter 68 — NV-CM tool SSO through the hub](../13-auth-integrations/68-nvcm-tool-sso-through-the-hub.md): how the Rendering console is reached.
- Customer view: [User guide ch. 27 — Network & server management](../../user-guide/04-feature-walkthroughs/27-network-and-server-management.md).
