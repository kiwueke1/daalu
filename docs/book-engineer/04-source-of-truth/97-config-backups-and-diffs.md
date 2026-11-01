# 97. Config backups, the backup scheduler, and version diffs

Chapter 96 covered the **intended** side of the Config Store: what a switch
*should* run, rendered from Nautobot. This chapter covers the **backup** side:
what a switch *does* run, pulled on a schedule or on demand, committed as a
version with who/when/why, compared with intent for drift, and diffed against
earlier pulls. It also states plainly how much of that reaches Daalu today
(less than chapter 21 implies).

> **Upstream vs Daalu.** The backup scheduler, `BackupWorkflow`, the Config
> Store and its history UI are **upstream NV-CM, outside Daalu's code**. Daalu
> installs them (the scheduler ships enabled in the chart), supplies the
> per-workflow RBAC without which nothing gets scheduled, and holds read
> clients for versions and diffs. Upstream references
> (`nv-config-manager/<path>:<line>`) are pinned to tag **`1.3.0`
> (`7455da6`)**, the commit Daalu vendors. Where the upstream tree has moved
> since (HEAD `2dad101`, `1.3.1-rc.2`) the text says so.

| Piece | Owner | Path |
|---|---|---|
| Backup scheduler (Temporal schedules) | upstream | `nv-config-manager/src/nv_config_manager/temporal/ngc/schedulers/backup.py` |
| `BackupWorkflow` (load → drift → persist) | upstream | `nv-config-manager/src/nv_config_manager/temporal/ngc/workflows/backup.py` |
| Persist-to-store activity | upstream | `nv-config-manager/src/nv_config_manager/temporal/ngc/activities/backup.py:49-73` |
| Version store + diff API | upstream | `nv-config-manager/src/nv_config_manager/config_store/` |
| History / compare UI | upstream | `nv-config-manager/ui/src/app/device/[uuid]/[filename]/history/page.tsx` |
| Scheduler deployment (on by default) | chart | `deploy/charts/nv-config-manager-1.3.0/values.yaml:1755-1757` |
| Workflow RBAC fix (makes scheduling possible) | Daalu | `src/daalu_automation/config_manager_controller/values.py:23-46`, `deployer_runner.py:159-166` |
| Version/diff client | Daalu | `src/daalu_automation/core/configmgr/client.py:170-196` |
| NATS → hub event bridge (not launched) | Daalu | `src/daalu_automation/workers/nats_bridge.py` |

---

## 97.1 A backup is a version, not a file

The Config Store keeps every version of every file per
`(device, filename, file_type)`; `file_type` is `intended` or `backup`
(`config_store/db/models.py:29-30`). A backup is written under the **same
filename as the device's intended config** — `startup.yaml` on Cumulus and
NV-OS, `full-config` on Arista EOS
(`temporal/common/mixins/device.py:177-185`, `backup_file` at `:210-212`) —
but with `file_type=backup`. That is why the Config browser shows a backup
under `leaf1 → startup.yaml` and why the history page carries a
`file_type` query parameter (`history/page.tsx:85`).

Three properties make the history useful:

- **No-op pulls write nothing.** A new version is created only when the
  content differs from the latest version
  (`config_store/core/storage.py:216-223`). A switch that hasn't changed
  produces one version, not one per schedule tick; the persist activity
  then returns the existing commit (`activities/backup.py:66-73`).
- **Every version carries attribution.** The commit message is
  `Backup trigger: <TRIGGER> User: <user>` (`workflows/backup.py:256`), with
  the trigger one of `SCHEDULED`, `SYSLOG`, `WORKFLOW`, `API`
  (`workflows/backup.py:63-69`). A backup with no user is refused outright
  (`workflows/backup.py:290-293`), so every version has an author.
- **Diffs are server-side.** `GET /v1/config/{device}/{file}/diff`
  (`config_store/api/config_v1.py:302-336`) returns a unified diff between
  two versions of the same file type. The UI's **Compare vN → vM** tab and
  its **Side-by-Side Comparison** card render that response
  (`history/page.tsx:182-183,316`).

Say "stored version vs stored version" on camera, not "live device diff":
both sides of that diff are store versions.

---

## 97.2 The scheduler: twice a day, with jitter

`BackupScheduler` (`temporal/ngc/schedulers/backup.py:53-222`) is a
reconcile loop, not a cron job. Every **10 minutes** (`backup.py:212-220`) it:

1. asks Nautobot for devices with `backup_enabled: true` whose
   `is_aggregate_managed` matches the environment (`backup.py:56-70`);
2. keeps only devices whose **status** is `Provisioned` or `Active` and whose
   **platform** is `Arista EOS`, `Cumulus Linux` or `NV-OS`
   (`backup.py:71-72`, filter at `:103-109`);
3. lists existing Temporal schedules with the `backup-` prefix
   (`backup.py:116-122`);
4. creates a schedule for each newly eligible device and deletes the
   schedule of each device that dropped out (`backup.py:182-199`).

Each per-device schedule (`backup.py:154-174`) starts `BackupWorkflow` with
`trigger=SCHEDULED`, user `nv-config-manager-temporal`, a 30-minute execution
timeout, on the spec in `backup.py:74-78`:

```python
SPEC = ScheduleSpec(
    intervals=[ScheduleIntervalSpec(every=timedelta(hours=12))],
    jitter=timedelta(hours=1),
)
```

**Every 12 hours, up to 1 hour of jitter.** The comment gives the reason:
jitter keeps every device from being pulled at the same moment. So the honest
phrasing is "twice a day", never "nightly". There is no per-site or
per-device interval knob at 1.3.0.

Design choice worth noting: the desired set lives in Nautobot, and the
scheduler converges Temporal toward it. Turning backups on for a switch is a
flag flip in Nautobot; nobody edits a schedule. If Nautobot is unreachable
the loop logs and **leaves schedules unchanged** (`backup.py:216-219`) rather
than deleting everything because the desired set came back empty.

### Why nothing got scheduled: the RBAC precondition

`schedule_device` looks up the RBAC roles for `BackupWorkflow` and, if there
are none, logs `No RBAC configuration found for BackupWorkflow` and
**returns without creating a schedule** (`backup.py:127-131`). The schedule
carries those roles as Temporal search attributes so the UI can filter who
may read or run it.

That is where Daalu comes in. The vendored installer computes the release's
`rbac.workflows` from a file path that does not exist in the controller image
and silently gets `[]`. Daalu reads `values-rbac-open.yaml` straight from the
vendored chart (`config_manager_controller/values.py:23-46`, wired at
`values.py:365-369`) and carries the result over the Deployer's empty answer
(`deployer_runner.py:159-166`). Without that, every workflow is greyed out in
the NV-CM UI *and* the scheduler can never create a backup schedule.

### What the creator should verify

The lab once logged `Scheduling 0 new device backups`. That line
(`backup.py:190`) counts the *desired* set, so zero means no device passed
the filter: check, in order,

1. a Config Manager Device record exists for the switch with
   `backup_enabled` on (all four plugin flags default to `False` — chapter 96
   §96.4; Daalu never creates these records);
2. the device's status is `Active` or `Provisioned`;
3. its platform name is exactly `Cumulus Linux` (the lab's
   `docs/content/lab/fabric-devices.yaml` uses platform `cumulus-linux`; check
   what Nautobot actually shows);
4. if the count is non-zero but no schedule appears, the RBAC log line above.

Treat the schedule as unproven until a version with message
`Backup trigger: SCHEDULED …` appears in the history.

---

## 97.3 `BackupWorkflow`: three stages

`BackupWorkflow` (`workflows/backup.py:90-326`, endpoint `/ngc/backup`) defines
three stages (`backup.py:106-123`), none of which requires approval:

| Stage | What it does |
|---|---|
| `load_running_configuration` | Connects to the device and pulls the running config (`backup.py:136-160`) |
| `check_drift` | Loads the latest **intended** config, runs a candidate diff of intended vs running on the device, records `has_drift` (`backup.py:175-232`) |
| `persist_backup` | Writes the running config as a new `backup` version, then records it on the device's backup row in the Nautobot plugin (`backup.py:251-286`) |

The first two run **in parallel** (`backup.py:296-307`); persist depends on
both.

Details that matter when you read a run:

- **Drift is not checked when a deploy started the backup.** If the input
  carries an `intended_config_commit_id` (a deploy just pushed that commit),
  `check_drift` short-circuits to "No drift detected"
  (`backup.py:178-185`).
- **A drift hit posts to Slack, nothing else.** When the diff is non-empty
  the stage renders "Configuration Drift Detected" with the diff and calls
  `send_slack_message` (`backup.py:210-224`), which only does something if
  Slack is configured in the environment. It does **not** open a change
  proposal, and it does not alert Daalu (§97.5).
- **"No drift" on a device you edited** means one of: the device has no
  intended config yet, or your edit happens to match intent.
- **No drift → the backup is linked to the intended commit**
  (`backup.py:309-312`), which is how the Nautobot plugin knows a device's
  deployed config is current.

### Who else starts it

- **Deploys.** `DeployWorkflow` ends with a `perform_backup` stage that starts
  `BackupWorkflow` as a child (`workflows/deploy.py:136`, `:298-310`). Daalu's
  executor budgets for it in its poll timeout
  (`core/device/config_manager.py:52-55`).
- **ZTP.** When a freshly provisioned switch calls `POST
  /v1/device/{uuid}/provisioned`, the ZTP service marks it `Provisioned` and
  invokes a backup (`ztp/api/device_v1.py:157-171`; chapter 98).
- **Humans and agents.** Workflows → **Configuration Backup** in the NV-CM UI,
  or the MCP tool `run_backup` (chapter 99) — `BackupWorkflow` is one of the
  MCP-enabled workflows (`backup.py:101`).

### Site-wide snapshots

A **Site Configuration Backup** workflow (`/ngc/site_backup`) that fans out
one `BackupWorkflow` child per in-scope device exists **only after 1.3.0**
(upstream HEAD `src/nv_config_manager/temporal/ngc/workflows/site_backup.py:120-130`,
added in upstream PR #135). Whether you have it depends on which source the
images were built from: Daalu's `deploy/scripts/config-manager/build-push-images.sh`
builds from whatever is checked out in `SRC_REPO` and tags it `1.3.0`
(`build-push-images.sh:34-35,52-57`) without checking out that tag. Check the
workflow list in the environment before scripting a site-wide take.

---

## 97.4 Secrets in backups

A running config contains secrets (password hashes, SNMP communities, keys).
**The Config Store, its API and the history UI return content as stored; no
redaction happens there.** The only redaction in the upstream codebase is in
the MCP server's response layer
(`nv-config-manager/src/nv_config_manager/mcp/clients.py:42-45,62-92`;
chapter 99 §99.4). So the same backup is masked when an agent reads it
through `get_device_config` / `get_config_diff`, and unmasked on the history
page. Crop or blur before recording.

---

## 97.5 What reaches Daalu (and what doesn't)

Be exact about this, because older chapters overstate it.

- **Read clients exist.** `ConfigStoreClient.list_versions` and `.diff`
  (`core/configmgr/client.py:170-196`) wrap the version and diff endpoints
  with `file_type` support. **No route, worker or agent tool calls them
  today**; the only `ConfigStoreClient` call site is the executor's
  `put_intended` (`core/device/config_manager.py:150-157`).
- **The executor does not read backups.** `ConfigManagerExecutor.collect`
  returns the intended hint unchanged and documents NV-CM as the drift
  authority, with drift meant to arrive over NATS
  (`core/device/config_manager.py:92-106`).
- **The NATS bridge is not running.** `workers/nats_bridge.py` maps
  `nv-config-manager.workflow.result` and the change subjects to hub
  `EventEnvelope`s (`nats_bridge.py:34-45`) and is unit-tested
  (`tests/test_nv_config_manager_integration.py:334`). But no process in this
  repository calls `run_all_bridges` (`nats_bridge.py:138`), and it only
  bridges tenants whose integration config has a `nats_url`
  (`nats_bridge.py:74-91`), a key nothing in the onboarding path writes.
  Chapter 21 §21.5 describes the bridge's intended behaviour.
- **The agent's change context is Kubernetes-only.** The `recent_changes`
  tool (`core/aiops/change_context.py`, chapter 86) reads rollouts and Argo CD
  Applications, not switch backups.

So today, backups and drift checks are visible in NV-CM's own console (opened
from the environment card through the hub tool proxy — `config-store` /
bare-host UI routes in `api/tool_proxy.py:97-103`) and over NV-CM's MCP
server, not in a native Daalu screen and not in Daalu alerts.

---

## 97.6 Failure modes

| Symptom | Cause | Where |
|---|---|---|
| `Scheduling 0 new device backups` | No device passes flag/status/platform filter | `schedulers/backup.py:56-72,103-109` |
| Non-zero to schedule, no schedules exist | Empty workflow RBAC | `schedulers/backup.py:127-131`; Daalu fix `values.py:23-46` |
| Run fails `Missing user for backup attribution.` | Started without a user (not through the API) | `workflows/backup.py:290-293` |
| `No drift detected` after a manual edit | No intended config, or the edit matches intent | `workflows/backup.py:195-212` |
| Compare tab disabled | Fewer than two versions (identical pulls are deduplicated) | `storage.py:216-223` |
| No Site Configuration Backup in the list | Images built from 1.3.0, which predates it | §97.3 |

---

## Related chapters

- [Chapter 96 — Config rendering and the render gate](96-config-rendering-and-render-gate.md): the intended side and the plugin flags.
- [Chapter 98 — Cable validation and ZTP](98-cable-validation-and-ztp.md): ZTP's backup on provision.
- [Chapter 99 — NV-CM's MCP server and its workflow allow-list](99-nvcm-mcp-server-and-workflow-allow-list.md): `run_backup` and redaction.
- [Chapter 21 — The NV-CM network plane](21-nvcm-network-plane.md) §21.5: the event bridge's design.
- [Chapter 23 — NV-CM internals](23-nvcm-internals-and-operator.md): the Config Store and Temporal services.
- [Chapter 68 — NV-CM tool SSO through the hub](../13-auth-integrations/68-nvcm-tool-sso-through-the-hub.md): how the Config browser is reached.
- The Kubernetes counterpart the agent does read: [Chapter 86 — Change context and previews](../15-ai-ops-maturity/86-change-context-and-previews.md).
