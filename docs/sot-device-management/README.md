# Source of Truth + Device Management — feature documentation

This subdirectory documents the SoT + device-management feature added to
daalu-automation across PRs #1–#9 — the SoT abstraction, executor
microservice, LLM `propose_change` tool, frontend approval UI, Linux /
Redfish / Junos / IOS-XR / EOS adapters, hosted-Nautobot provisioning,
and per-device credential resolution. Read these in the order listed below.

## What the feature gives you

Before this feature, daalu-automation could **observe** infrastructure
(AWS/GCP/Azure logs, Prometheus, Loki, k8s) and **actuate the k8s cluster**
via the alert-chat agent. With it, daalu can now:

1. **Know the canonical state of every managed device** — servers, BMCs, network
   gear — via a Source of Truth (Nautobot).
2. **Detect drift** between what the SoT says a device should be and what it
   actually is. Differences become `ChangeProposal` rows.
3. **Let the AI engine propose config changes** during alert investigations,
   complete with reasoning and the supporting evidence (which logs, metrics,
   and alerts drove the proposal).
4. **Push approved changes** to the real device — but never directly from the
   engine. Every push is operator-approved, identity-gated, and stale-checked.

The architectural through-line:

```
AI engine / user / drift reconciler
         │ propose
         ▼
   ChangeProposal (pending)
         │ operator approves in /proposals UI
         ▼
   ChangeProposal (approved)
         │ executor service picks up on next beat tick
         ▼
   Device adapter executes — config pushed to real device
         │
         ▼
   ChangeProposal (executed | failed | stale)
```

The engine code can call `change_proposals.propose()` but **cannot** call
`change_proposals.execute()`. Three independent layers — code gate, Celery
queue subscription, k8s ServiceAccount — keep that boundary.

## Read in this order

| Doc | What's in it |
| --- | --- |
| [`architecture.md`](architecture.md) | The full pipeline, every component, the locked architectural decisions, and the data flows |
| [`setup-guide.md`](setup-guide.md) | Operator-side deploy: settings, k8s manifests, secrets, bootstrap order |
| [`nautobot-guide.md`](nautobot-guide.md) | Spinning up Nautobot (hosted-by-you or BYO), the data model we expect, custom fields, webhooks, provisioning |
| [`onboarding-guide.md`](onboarding-guide.md) | Per-tenant onboarding: wizard flow, integrations, adding devices, per-device credentials |
| [`adapter-reference.md`](adapter-reference.md) | LinuxSSH / Redfish / Network adapters — what each manages, transports, examples, how to extend |
| [`api-reference.md`](api-reference.md) | Every new HTTP route this feature added — list, get, approve, reject, webhooks, provisioning, hosted-status |

## At a glance — what each PR added

| PR | Branch | Summary |
| -- | ------ | ------- |
| 1 | `pr1-sot-foundation` | `SoT` ABC + `NautobotSoT`; `DeviceAdapter` ABC + `LinuxSSHAdapter`; `ChangeProposal` model + service layer + HTTP routes; Nautobot webhook receiver; drift reconciler beat task. 11 tests. |
| 2 | `pr2-executor-service` | Executor microservice — dedicated `executor` Celery queue, `daalu-executor` k8s Deployment with its own ServiceAccount, `daalu executor` CLI command. The single thing that calls `change_proposals.execute()`. 6 tests. |
| 3 | `pr3-propose-change-tool` | `propose_change` LLM tool — engine can author proposals during alert investigations with structured reasoning + evidence. 7 tests. |
| 4 | `pr4-approval-ui` | Frontend `/proposals` list + detail page; evidence panel renders LLM reasoning, model name, confidence, linked events / alerts / metrics; Approve / Reject buttons. |
| 5 | `pr5-redfish-adapter` | `RedfishAdapter` for server BMCs (BIOS / boot order / power state). Widens `DeviceFacts` to a `LinuxFacts \| RedfishFacts` union; NautobotSoT dispatches parsing by `device.transport`; `redfish_credentials` Integration slot. 13 tests. |
| 9 | `pr9-per-device-creds` | Multiple credential rows per provider per tenant, with priority resolution: per-device row > named `_ref` > tenant-wide fallback. Applies symmetrically to ssh / redfish / network credentials. 9 tests. |
| 7 | `pr8-nautobot-provisioning` | Hosted-Nautobot provisioning route (`POST /onboarding/nautobot/provision`) — idempotently creates the Nautobot Tenant + ObjectPermission + APIToken for the daalu tenant. Nautobot onboarding step (catalog 13 → 14). 9 tests. |
| 6 | `pr6-network-adapters` | Adapters for Junos / IOS-XR / Arista EOS — NETCONF (scrapli-netconf) for Junos & IOS-XR with commit-confirmed semantics, Scrapli CLI for EOS. `NetworkFacts` schema (hostname / interfaces / VLANs / static routes), `network_credentials` Integration slot. 28 tests. |

> **PR numbering note:** PRs 1–10 are the *roadmap* numbers. The GitHub PR
> IDs drift because some merges landed under different sequential numbers
> (e.g., the network-adapters work merged as GitHub PR #9, not #6).
> Cross-reference by branch name when in doubt.

## Key concepts and where they live

| Concept | Lives in |
| ------- | -------- |
| Source of Truth abstraction | `core/sot/base.py` (ABC), `core/sot/nautobot.py` (impl) |
| Source-of-Truth facts schemas | `core/sot/models.py` — `LinuxFacts`, `RedfishFacts`, `NetworkFacts`, `DeviceFacts` union |
| Device adapters | `core/device/{base,linux_ssh,redfish,junos,iosxr,eos}.py` |
| ChangeProposal lifecycle | `core/change_proposals.py` — `propose`, `approve`, `reject`, `mark_stale`, `execute` |
| Per-tenant credential resolution | `core/change_proposals.py` — `resolve_credentials`, `_select_credentials_row` |
| Reconciler (drift detection) | `workers/reconciler.py` — Celery beat task |
| Executor (approved → pushed) | `workers/executor.py` + `deploy/k8s/workers/executor.yaml` |
| Hosted-Nautobot provisioning | `core/sot/nautobot_provisioning.py` + `api/routers/onboarding.py` |
| HTTP routes for proposals | `api/routers/change_proposals.py` |
| Webhook receiver | `api/routers/sot_webhooks.py` |
| LLM tool for proposing changes | `core/sot_tools.py` (merged into `core/kube_tools.py` registry) |
| Frontend proposals UI | `frontend/app/proposals/` |

## Operating principles (the spirit of the design)

1. **The engine never pushes.** It can read and propose. Pushing requires
   a separate identity (executor JWT scope) that the engine doesn't carry.
2. **Approval is the only path to a real-device change.** Drift detection,
   LLM proposals, and operator-initiated changes all land as `pending`
   ChangeProposals — even drift, so an operator can decide whether to
   re-converge or update intent to match reality.
3. **Stale-detection is automatic.** If the SoT intent changed between
   approval and execution, the executor flips the proposal to `stale`
   rather than push state the operator didn't approve.
4. **One Nautobot per platform deploy, isolated by ObjectPermission.**
   We do not run a separate Nautobot instance per customer. The security
   boundary is the Nautobot ObjectPermission constraint
   `{tenant__slug: <slug>}` — a bug there is a cross-tenant data leak.
5. **Tenants can BYO their own Nautobot.** The whole feature works with a
   customer-managed Nautobot URL+token; hosted is just one provisioning path.
6. **Adapters are narrow on purpose.** Each adapter manages 3–5 well-defined
   facts. Anything beyond that is a follow-up PR per fact family — we never
   try to model "everything Linux" or "everything IOS-XR" in one go.

## UI surface

| Path | Page | Purpose |
| ---- | ---- | ------- |
| `/onboarding` | Generic 14-step wizard | Includes the Nautobot step as one of the standard integrations |
| `/onboarding/sot` | **SoT-specific wizard** | 3-step guided setup: pick BYO vs hosted → wire connection → seed SSH credentials. Funnels to `/devices` at the end |
| `/devices` | Devices list | Lists every device from the SoT with an Add-device form (talks to Nautobot through the daalu API) |
| `/devices/[id]` | Device detail + intent editor | Form-based editor for `LinuxFacts` (the common case); JSON editor for Redfish / Junos / IOS-XR / EOS until per-transport forms ship |
| `/proposals` | Pending approvals | Operator review surface for every push (drift, manual, engine-authored) |
| `/proposals/[id]` | Proposal detail | Diff + evidence panel + Approve/Reject |

## Where this fits in the broader codebase

Daalu's existing pillars before this feature:

- **Integrations** (`core/integrations.py`) — ingest data from external systems
- **Agents** (`core/agents.py`) — long-running consumers reacting to the event stream
- **Alerts** (`models/alert*.py`) — promoted operational signals with dedupe + chat
- **K8s actuation** (`core/kube_tools.py`) — the engine's tool surface for cluster ops

The SoT / device-management feature plugs in:

- A **new actuation surface** alongside k8s tools, but with a stricter approval contract
- A **new agent capability** (`propose_change` tool) merged into the same tool registry
- A **new data layer** (`ChangeProposal` model) joining `Alert`, `AlertAction`, `Event`
- A **new Source-of-Truth abstraction** that other modules can consume later (e.g., the cloud modules could read intended cloud-resource state from the same SoT in a future iteration)
