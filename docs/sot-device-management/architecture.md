# Architecture — SoT + device management

## The big picture

```
                    ┌─────────────────────────────────────────────────────┐
                    │                  S O U R C E S                       │
                    │                                                       │
       ┌──── AWS CloudWatch ────┐  ┌── Prometheus / Loki ──┐  ┌── K8s ──┐ │
       │   Cloud APIs (GCP/Az)  │  │  Thanos / OpenSearch  │  │  events │ │
       └────────────┬───────────┘  └───────────┬───────────┘  └────┬────┘ │
                    │                          │                   │      │
                    └──────────────┬───────────┴───────────────────┘      │
                                   │   event stream (Redis xstream)        │
                                   ▼                                       │
       ┌─────────────────────────────────────────────────────────────────┐│
       │                    A L E R T   E N G I N E                       ││
       │   alert fingerprinting · dedupe · LLM triage chat                ││
       └───────────────────────────────┬─────────────────────────────────┘│
                                       │                                   │
                          tool calls during investigation                  │
                                       │                                   │
                    ┌──────────────────┴────────────────────────┐          │
                    ▼                                            ▼          │
         (existing) kube tools                       NEW  propose_change   │
            scale, restart, rollback                      tool             │
            patch_resource, etc.                                           │
                    │                                            │          │
                    │ requires_approval=True                     │          │
                    │ goes through AlertAction (PR 1 pattern)    │          │
                    │                                            │          │
                    ▼                                            ▼          │
              Approve in chat                       NEW  ChangeProposal     │
              → kubectl push                            (pending)            │
                                                            │                │
                                                            │                │
                                                            ▼                │
                                                      Operator approves      │
                                                       in /proposals UI       │
                                                            │                │
                                                            │ approved        │
                                                            ▼                │
                                                  NEW  Executor service     │
                                                  (daalu-executor pod)       │
                                                  - mints executor actor    │
                                                  - re-renders intent       │
                                                  - re-checks stale         │
                                                  - calls adapter.execute() │
                                                            │                │
                                                            ▼                │
                                            ┌──────────────────────────────┐│
                                            │   NEW   D E V I C E S         ││
                                            │                                ││
                                            │ Linux servers  (SSH adapter)   ││
                                            │ Server BMCs    (Redfish)       ││
                                            │ Network gear:                  ││
                                            │   Junos NETCONF, IOS-XR        ││
                                            │   NETCONF, EOS Scrapli CLI     ││
                                            └──────────────┬─────────────────┘│
                                                           │                   │
                                                           ▼                   │
                                                  observed state           │
                                                  reported back            │
                                                           │                   │
                                       ▼ webhooks         │                   │
                              ┌────────────────────────┐  │                   │
                              │  N A U T O B O T       │  │                   │
                              │  Source of Truth       │◀─┘                   │
                              │                        │                       │
                              │  intended_config       │     drift            │
                              │  per device            │   reconciler         │
                              │  (Config Context)      │  (every 5 min)       │
                              │                        │      │               │
                              │  webhook on update     │      │ compares      │
                              └────────┬───────────────┘      │ → proposes    │
                                       │                      │ drift         │
                                       ▼                      │               │
                              /api/v1/sot/webhooks/{tenant}   │               │
                              HMAC-SHA512 verified           │                │
                              event published                │                │
                                                              │                │
                                                              ▼                │
                                                  ChangeProposal (drift)      │
                                                              │                │
                                                              └────────────────┘
                                                                  back to top
```

The shape: **events flow up, intent flows down, and every push to a real
device crosses an approval gate.**

## Component-by-component

### Source of Truth abstraction — `core/sot/`

The SoT layer is an interface, not a Nautobot client. The rest of the
codebase (engine, reconciler, executor, UI) talks to `SourceOfTruth`;
`NautobotSoT` is the only implementation that ships. Customers can BYO
their own Nautobot or use one we host — either way it's the same
adapter.

**Why an abstraction:**
- Lets BYO and hosted modes share all the engine/executor code without
  branching
- Keeps Nautobot's API quirks out of every consumer
- Doesn't lock us into Nautobot — we kept the ABC after dropping
  NetBox support so a future re-eval
  is cheap

**Methods (`core/sot/base.py`):**
```python
class SourceOfTruth(ABC):
    async def list_devices(db, tenant_id, *, platform=None) -> list[Device]
    async def get_device(db, tenant_id, device_id) -> Device | None
    async def get_intended_config(db, tenant_id, device_id) -> IntendedConfig | None
    async def put_intended_config(db, tenant_id, device_id, intended) -> SoTRevision
    async def record_observed(db, tenant_id, observed) -> None
    async def health(db, tenant_id) -> (bool, str)
```

**`NautobotSoT` implementation:**
- Reads via GraphQL (`POST /api/graphql/`)
- Writes via REST (`/api/extras/config-contexts/`) because Nautobot's
  GraphQL mutations aren't enabled stock
- Per-tenant credentials read from `Integration(provider="nautobot")`,
  decrypted on the fly
- Intent stored as a Config Context named `daalu_intent` attached to
  the Device; observed snapshots as `daalu_observed`

### Facts schemas — `core/sot/models.py`

Each adapter family has a Pydantic schema for what it manages:

```python
LinuxFacts   = hostname + authorized_keys + sysctl + packages + cloud_init
RedfishFacts = bios_attributes + boot_override + power
NetworkFacts = hostname + interfaces + vlans + static_routes
```

These are unioned:

```python
DeviceFacts = LinuxFacts | RedfishFacts | NetworkFacts
```

`IntendedConfig.facts` and `ObservedSnapshot.facts` are typed against
this union. The NautobotSoT layer chooses which concrete type to
instantiate based on `device.transport` — pydantic doesn't auto-decide
(no discriminator field), the dispatch is explicit. That way existing
Nautobot Config Context blobs don't need to grow a `kind` field.

### Device adapter family — `core/device/`

```python
class DeviceAdapter(ABC):
    transport: ClassVar[str]
    async def collect(creds, intended_hint=None) -> DeviceFacts
    async def render(intended) -> RenderedConfig
    async def diff(observed, intended) -> ConfigDiff
    async def execute(creds, rendered) -> ExecutionResult
```

Three rules every adapter follows:

1. **Manage only what intent specifies.** Never enumerate the device's
   full config and treat absence-in-intent as "delete". For Linux,
   `sysctl` keys not in intent are left alone. For BMC, BIOS attrs not
   in intent are left alone. Same for network gear.
2. **Render must be deterministic.** The executor's stale-detection
   compares the `rendered.files` mapping byte-for-byte; any
   non-determinism (dict-ordering, timestamps in payloads) makes drifts
   that aren't real.
3. **`execute()` is the only method `change_proposals.execute()` calls.**
   Everything else (collect / render / diff) is read-only and used by
   the reconciler and the LLM tool freely.

Each adapter registers itself at import time via
`register_device_adapter(...)`. `core/device/__init__.py` side-effect-
imports each concrete adapter so the registry is populated when
anything imports the `device` package.

### ChangeProposal — `core/change_proposals.py`

The lifecycle row. Every push goes through it:

```
pending ─── approve ──▶ approved ─── execute ──▶ executed
   │                       │                        │
   │                       └── execute ──▶ stale    │
   │                                                │
   │                       └── execute ──▶ failed   │
   │                                                │
   └── reject ──▶ rejected                          │
```

`propose()` — anyone may call (engine, reconciler, user, importer).
Returns a new `pending` row.

`approve()` / `reject()` — operator action, requires the row be
`pending`.

`execute()` — the chokepoint. **Refuses unless:**
1. `actor.kind == "executor"` AND `actor.scope == settings.executor_jwt_scope`
2. `proposal.status == "approved"`
3. Re-rendered intent from the SoT matches the snapshot taken at
   propose time (otherwise flips to `stale`)

`mark_stale()` — service-layer helper if the row needs to be
invalidated explicitly (e.g., by a future webhook handler that
detects intent change).

### Reconciler — `workers/reconciler.py`

Celery beat task `sot.reconcile_devices` running every
`settings.sot_reconcile_period_s` seconds (default 300). For each
tenant that has both a `nautobot` and a credentials integration:

1. `sot.list_devices(platform="linux")` (today; tomorrow extends to
   redfish + network)
2. For each device with no open ChangeProposal:
   1. Pull intent via `sot.get_intended_config(...)`
   2. Pull observed via `adapter.collect(...)`
   3. Diff. If different → `propose(kind="drift", ...)`

Tenants without integrations are silently skipped — onboarding gates
are elsewhere. Per-device errors don't poison the sweep.

### Executor — `workers/executor.py` + `deploy/k8s/workers/executor.yaml`

Celery beat task `sot.execute_approved` running every
`settings.executor_period_s` seconds (default 30). **Routed via
`celery_app.conf.task_routes` to a dedicated `executor` queue** that
the main worker pool doesn't subscribe to.

Each tick:

1. `SELECT * FROM change_proposals WHERE status='approved' AND executed_at IS NULL ORDER BY approved_at LIMIT batch_size`
2. For each row:
   1. Look up the device on the SoT to get its `transport`
   2. Resolve the right `DeviceAdapter` via the registry
   3. Resolve the device's `Credentials` via `change_proposals.resolve_credentials(...)`
   4. Mint `Actor(kind="executor", scope=settings.executor_jwt_scope)`
   5. Call `change_proposals.execute(...)` — the function does the
      stale-check + adapter.execute()

The k8s Deployment is **single-replica on purpose**. Adding pods
would create racing executor identities on the same approved rows.
Throughput is scaled inside the process via `--concurrency=N`.

### Per-tenant credentials — `change_proposals.resolve_credentials`

Three-step resolution (added in PR 9):

1. **Per-device row:** `Integration.config.device_id == device.id`
   wins. Operators carve out high-trust devices with a dedicated key.
2. **Named-credential pointer:** `device.extra["{provider}_ref"]`
   (`ssh_credentials_ref`, `redfish_credentials_ref`,
   `network_credentials_ref`) names an Integration row by `name`.
   Useful for shared pools (jump host, BMC management VLAN).
3. **Tenant-wide fallback:** any row without `device_id`. Usually
   exactly one.

A ref pointing at a non-existent row → `LookupError` (loud failure,
not a silent fallback). Per-device user / port overrides via
`device.extra.{ssh_user, ssh_port, redfish_user, redfish_port,
network_user, network_port}` apply on top of the *selected* row.

### LLM `propose_change` tool — `core/sot_tools.py`

Merged into the alert-chat tool registry (`core/kube_tools.py
TOOLS`) the same way cloud read-tools are merged. **`requires_approval=False`**
intentionally — the `ChangeProposal` itself is the approval surface,
and double-gating would fork authority across two UIs.

The handler:
1. Validates `intended_facts` against the right facts schema (dispatches
   by the target device's transport — `LinuxFacts`, `RedfishFacts`, or
   `NetworkFacts`)
2. Looks up the device on the SoT
3. Renders the proposed intent via the right adapter
4. Diffs vs. current SoT intent
5. Writes a `pending` ChangeProposal with `Actor(kind="engine",
   name="infra-agent")` and structured evidence:
   - `triggered_by: "engine"`
   - `llm_reasoning` — the model's 1–3 sentence explanation
   - `llm_model` — which Claude model produced it
   - `evidence_events / evidence_alerts / evidence_metrics` — what
     drove the decision
   - `confidence` — model's self-reported 0–1

The frontend renders all of this verbatim on the approval card so the
operator isn't approving blind.

### Webhook receiver — `api/routers/sot_webhooks.py`

`POST /api/v1/sot/webhooks/{tenant_slug}` — Nautobot pushes here on
intent changes. **HMAC-SHA512** verification against the per-tenant
webhook secret stored in the Nautobot integration row. Emits a
`sot.intent.changed` event onto the stream so other components can
react (e.g., a future "auto-stale all pending proposals for this
device" handler).

Tenant-slug-in-URL is intentional: Nautobot can't include arbitrary
auth headers easily, and it's the same pattern as the existing
per-tenant ingest webhook. The whole path is exempted from
`AuthGateMiddleware` via the route's regex bypass.

### Frontend — `frontend/app/proposals/`

Two pages:

- **`/proposals`** — tabbed list (Pending default, then Approved,
  Executed, Failed, Stale, Rejected, All). Polls every 10s.
- **`/proposals/[id]`** — detail. Five panels: header (status / kind /
  timestamps), action bar (Approve / Reject when pending), evidence
  (the LLM's reasoning + linked events + metrics + confidence), diff
  (unified diff with `+/-/@@` coloring), executor result (success /
  rollback flag / per-step trace).

Sidebar nav adds `/proposals` between `/alerts` and `/integrations`
with the `GitPullRequest` icon — reads as "review and merge" semantics
for ops users.

## Locked architectural decisions and why

| Decision | Chose | Why |
| --- | --- | --- |
| SoT product | **Nautobot** behind a `SourceOfTruth` ABC | Don't reinvent DCIM/IPAM (years of schema work); the abstraction keeps us un-locked-in. Single Nautobot + per-tenant ObjectPermissions is the supported SaaS pattern |
| Server transport first | **SSH, then Redfish** | Smallest transport surface; easiest to dogfood; proves the approval-push loop end-to-end before fighting vendor parsers |
| Push model | **Approval-gated, GitOps-style** | Engine never pushes. Every change goes `propose → approve → execute`. Three independent layers (code gate, queue subscription, k8s SA) enforce it |
| Rendered config storage | **Snapshotted on the proposal row at propose time** (Decision 4) | Deterministic: the diff the user approves is exactly what gets pushed. Stale-check at execute time catches drift between approve and execute |
| LLM evidence in proposals | **Every proposal carries structured `evidence`** | Operators approving blind is the failure mode that kills trust. `triggered_by + llm_reasoning + llm_model + evidence_events + confidence` go onto every row and render verbatim in the UI |
| Executor runtime | **Celery beat task on a dedicated queue** | Matches the codebase's `wireguard_health` / `reconciler` patterns. 5–60s approve→execute latency acceptable since humans approve |
| `propose()` over HTTP | **Python-only in v1** (no `POST /change-proposals` create route) | External proposers (importers, third-party systems) need their own auth+audit shape that hasn't been designed. The immediate need is the LLM tool (in-process); HTTP can land later |
| Per-tenant Nautobot strategy | **One Nautobot, per-tenant `Tenant` + `ObjectPermission` constraints** | Operational simplicity. Security boundary is the constraint dict `{tenant__slug: <slug>}`. Bug there = data leak; the security-boundary test pins the constraint exactly |
| NetBox SoT support | **Dropped** | Slot kept in the abstraction layer as a test seam. |
| Git-backed config storage | **Deferred** | Long-tail. Once PRs 1–9 + 6 have shaken out, this becomes the natural next thing if you want PR-style review on every SoT change |

## Identity boundaries — the security model

The single most important property of this feature is that **the AI engine
cannot push to a real device on its own**. Three independent layers enforce it:

### Layer 1 — Code gate (`change_proposals.execute()`)

```python
if actor.kind != "executor" or actor.scope != settings.executor_jwt_scope:
    raise PermissionError(...)
```

The engine constructs `Actor(kind="engine", ...)`. Users authenticate as
`Actor(kind="user", ...)`. Only the executor service mints
`Actor(kind="executor", scope=settings.executor_jwt_scope)` — and it does
so directly in Python (it runs with that JWT, doesn't authenticate to mint).
A prompt-injection attack that smuggles `execute()` calls into the engine's
code path fails this check at runtime.

### Layer 2 — Celery queue subscription

`celery_app.conf.task_routes` routes `sot.execute_approved` to
`settings.executor_queue_name`. The main worker pool (`daalu-worker`)
consumes the default queue and **never subscribes** to `executor`. Even if
malicious code imports `workers.executor` and triggers the task, the message
sits in the executor queue unconsumed unless a pod running
`daalu executor --queues=executor` exists.

### Layer 3 — K8s ServiceAccount + env

`daalu-executor` Deployment runs as its own `ServiceAccount` with its own
`EXECUTOR_JWT_SCOPE` env var. Ops can revoke executor capability at the
cluster level without touching the rest of the platform. If a deploy
accidentally mis-applies the executor SA to a different pod, the missing
env var fails Layer 1.

### Stale-check — Layer 3.5

Even after passing all three layers, `change_proposals.execute()`:

1. Re-renders intent fresh from the SoT
2. Compares byte-for-byte to the snapshot taken at propose time
3. If different → flips the row to `stale`, raises `StaleProposalError`,
   **does not call `adapter.execute()`**

This catches the case where intent changed between approval and execution
— e.g., another operator updated the SoT, or the LLM proposed a different
change after the original approver left for the day.

## Data flows — three scenarios

### Scenario A — Drift detection auto-converges

1. `T=0` — reconciler beat tick. For tenant `acme`:
2. Walks every linux device in their Nautobot. For each:
   - `sot.get_intended_config()` returns `LinuxFacts(hostname=web01,
     sysctl=[vm.swappiness=10])`
   - `adapter.collect()` returns `LinuxFacts(hostname=other-host, sysctl=[])`
   - Diff: hostname changed + sysctl missing
3. Writes `ChangeProposal(kind="drift", evidence={triggered_by:
   "reconciler", facts_changed: ["hostname", "sysctl.vm.swappiness"]})`
   in `pending` status.
4. UI surfaces it in `/proposals` with a "Drift reconciler" badge.
5. Operator clicks Approve. Status flips to `approved`.
6. `T+30s` — executor tick. Picks up the approved row. Re-renders intent
   from SoT (still hostname=web01) → matches the snapshot → calls
   `LinuxSSHAdapter.execute()`. SSH connection, `hostnamectl set-hostname web01`,
   write `/etc/sysctl.d/99-daalu.conf`, `sysctl --system`. ExecutionResult
   success. Row → `executed`.

### Scenario B — LLM proposes during alert investigation

1. PagerDuty / Prometheus alert fires: "HighSwapUsage on web01"
2. Operator opens `/alerts/<id>`. The alert-chat agent triages
   automatically (`triggerTriage` if no assistant message exists).
3. Claude calls `query_prometheus(...)` and `get_pod_events(...)` to
   investigate.
4. Claude concludes "swappiness=60 default is biasing toward swap; should
   be 10 for this workload" and calls `propose_change(device_id="<web01
   nautobot id>", intended_facts={"sysctl":
   [{"name":"vm.swappiness","value":"10"}]}, llm_reasoning="OOM events
   correlated with default swappiness=60; reducing to 10 to bias toward
   keeping working set resident.", evidence_alerts=["<alert id>"],
   evidence_metrics=[{"name":"node_memory_SwapFree","value":"0"}])`
5. The tool handler:
   - Looks up the device
   - Renders the proposed intent
   - Diffs vs. current SoT intent (none → diff is the full rendered file)
   - Writes a `ChangeProposal(kind="manual", evidence={triggered_by:
     "engine", llm_reasoning: "...", llm_model: "claude-sonnet-4-6",
     evidence_alerts: [...], evidence_metrics: [...], confidence: 0.82})`
   - Returns "Created ChangeProposal <id>" to the chat
6. Operator reviews. The Proposals UI surfaces the reasoning + the
   evidence + the diff. They click Approve.
7. Executor picks up on next tick and pushes.

### Scenario C — Operator updates SoT directly (Nautobot UI)

1. Operator edits a device's `daalu_intent` Config Context in Nautobot's
   web UI (or via Nautobot's own API).
2. Nautobot fires a webhook to `POST /api/v1/sot/webhooks/{tenant_slug}`
   with HMAC signature.
3. Our handler verifies the HMAC, parses the payload, publishes a
   `sot.intent.changed` event onto the Redis stream.
4. Drift reconciler will catch it on its next tick and propose a drift
   proposal if observed ≠ new intent.
5. (Future: a webhook handler that auto-stales any pending proposals
   for the device, so they're re-authored against the new baseline.)

## Schema additions

This feature added one table: `change_proposals` (PR 1).

```sql
CREATE TABLE change_proposals (
    id              UUID PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_id       VARCHAR(255) NOT NULL,
    kind            VARCHAR(32) NOT NULL,            -- drift | manual | intended_change
    status          VARCHAR(32) NOT NULL,            -- pending | approved | rejected | executed | failed | stale
    intended_config TEXT NOT NULL,
    observed_config TEXT NOT NULL,
    diff            TEXT NOT NULL,
    renderer_version VARCHAR(64) NOT NULL,
    evidence        JSONB NOT NULL DEFAULT '{}',
    created_by      UUID,                             -- user id when actor.kind="user"
    approved_by     UUID,                             -- who clicked Approve / Reject
    approved_at     TIMESTAMPTZ,
    executed_at     TIMESTAMPTZ,
    executor_result JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_change_proposals_tenant_status ON change_proposals (tenant_id, status);
CREATE INDEX ix_change_proposals_tenant_device ON change_proposals (tenant_id, device_id);
```

Plus rows in the existing `integrations` table for the new providers:

- `provider="nautobot"` — `{url, token_ciphertext, webhook_secret_ciphertext, nautobot_tenant_slug, nautobot_tenant_id, permission_id?, hosted?: bool}`
- `provider="ssh_credentials"` — `{user, port, sudo, private_key_ciphertext?, password_ciphertext?, device_id?}`
- `provider="redfish_credentials"` — `{user, port, verify_tls?, password_ciphertext, device_id?}`
- `provider="network_credentials"` — `{user, port, password_ciphertext, enable_password_ciphertext?, private_key_ciphertext?, device_id?}`

`device_id?` on the credential rows enables PR 9's per-device priority
resolution.

## Settings reference (the new knobs)

| Setting | Default | What it controls |
| ------- | ------- | ---------------- |
| `sot_reconcile_period_s` | `300` | Drift reconciler cadence (seconds) |
| `executor_jwt_scope` | `"executor"` | Scope claim that `Actor(kind="executor")` must carry |
| `executor_period_s` | `30` | Executor beat cadence — bounds approve→execute latency |
| `executor_batch_size` | `25` | Max approved rows the executor processes per tick |
| `executor_queue_name` | `"executor"` | Celery queue the executor task is routed to |
| `managed_nautobot_url` | `""` | If set, hosted-Nautobot provisioning is enabled |
| `managed_nautobot_admin_token` | `""` | Admin token used by the provisioning route |
| `managed_nautobot_service_user` | `"daalu-platform"` | Nautobot user our APITokens are minted *for* |
| `commit_confirmed_timeout_s` | `600` | Junos / IOS-XR commit-confirmed window (seconds) |

## What this feature does NOT do

Worth being explicit about the not-yet shape:

- **No automatic push to devices.** Drift detection only proposes; an
  operator must approve. (We deliberately did not build an "auto-approve
  trivial drifts" mode in v1.)
- **No firmware updates.** Redfish adapter explicitly skips
  `UpdateService.SimpleUpdate` — needs multi-step task polling.
- **No virtual media mount, SEL log ingest, BMC firmware.**
- **No "delete unmanaged" semantics.** If you stop managing an interface
  / sysctl / BIOS attr in intent, the device keeps whatever value was
  there. Tear-down is an explicit operator action.
- **No `POST /change-proposals` HTTP route to create proposals externally.**
  v1 keeps creation Python-only (engine tool, reconciler, importer would
  call into Python).
- **No frontend for hosted-Nautobot "Provision" button.** The route
  exists; the wizard UI is a follow-up. Operators `curl` for now.
- **No frontend for managing per-device credential rows.** SQL / API
  direct for now.
- **No NetBox SoT.** Dropped intentionally.
- **No git-backed config storage** (Decision 4b deferred). Snapshotted on
  the row itself today.
