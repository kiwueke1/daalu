# Setup guide — operator side

This guide is for the platform operator (you, deploying daalu-automation
itself). For per-tenant onboarding (the customer-facing flow), see
[`onboarding-guide.md`](onboarding-guide.md).

## Prerequisites

Already deployed (pre-feature):

- Postgres (the `daalu_automation` DB) — Alembic migrations apply on
  bootstrap
- Redis (Celery broker + event stream)
- The daalu-automation app image (`ghcr.io/kiwueke1/daalu-automation:latest`)
  with the existing `daalu-api`, `daalu-worker`, `daalu-beat`, `daalu-agents`
  Deployments + namespace + secret

New for this feature:

- **A `daalu-executor` Deployment** (with its own ServiceAccount + env)
- **A Nautobot instance** — either hosted by you (one shared multi-tenant
  install) or tenants bring their own. If hosting, see [`nautobot-guide.md`](nautobot-guide.md)
- A few new settings in `daalu-automation-secrets`

## Settings reference (the new knobs)

Add to your env / secret:

```bash
# ── Source of Truth + reconciler ─────────────────────────────────────
SOT_RECONCILE_PERIOD_S=300                # drift detector cadence

# ── Executor service (PR 2) ─────────────────────────────────────────
EXECUTOR_JWT_SCOPE=executor               # gate the executor identity check
EXECUTOR_PERIOD_S=30                      # beat tick — bounds approve→execute latency
EXECUTOR_BATCH_SIZE=25                    # max rows per tick
EXECUTOR_QUEUE_NAME=executor              # Celery queue the executor consumes

# ── Hosted Nautobot (PR 8) — only if you host Nautobot ──────────────
MANAGED_NAUTOBOT_URL=https://nautobot.daalu.example.com
MANAGED_NAUTOBOT_ADMIN_TOKEN=...          # Nautobot superuser token
MANAGED_NAUTOBOT_SERVICE_USER=daalu-platform

# ── Network adapters (Junos / IOS-XR commit-confirmed) ─────────────
COMMIT_CONFIRMED_TIMEOUT_S=600            # Junos / IOS-XR rollback window
```

Leave the `MANAGED_NAUTOBOT_*` empty if you don't host Nautobot — the
hosted-status endpoint reports off, the wizard hides the Provision
button, and tenants must BYO their own Nautobot URL + token.

## Bootstrap order

1. **Apply migrations.** The `change_proposals` table is migration `0005`.
   ```bash
   alembic upgrade head
   ```
2. **Apply k8s namespace + RBAC + secret.**
3. **Apply the worker manifests in order.**
   ```bash
   kubectl apply -f deploy/k8s/workers/deployment.yaml      # existing: worker + beat + agents
   kubectl apply -f deploy/k8s/workers/executor.yaml        # NEW
   ```
4. **Verify the executor pod is in the `executor` queue.**
   ```bash
   kubectl logs -n daalu-automation deploy/daalu-executor | head -5
   # Expect to see: "celery@executor@..." with concurrency=2 and "[queues]" listing "executor"
   ```
5. **Verify the beat scheduler emits `sot.execute_approved`.**
   ```bash
   kubectl exec -n daalu-automation deploy/daalu-beat -- celery -A daalu_automation.workers.celery_app inspect registered | grep sot.execute_approved
   ```
6. **If hosting Nautobot:** deploy Nautobot + create the
   `daalu-platform` service user. See [`nautobot-guide.md`](nautobot-guide.md).

## K8s manifest reference

### Existing (pre-feature)

| File | Deployment | Replicas | Image | Cmd |
| ---- | ---------- | -------- | ----- | --- |
| `deploy/k8s/workers/deployment.yaml` | `daalu-worker` | 2 | `ghcr.io/.../daalu-automation:latest` | `daalu worker --concurrency=4` |
| `deploy/k8s/workers/deployment.yaml` | `daalu-beat` | 1 | same | `daalu beat` |
| `deploy/k8s/workers/deployment.yaml` | `daalu-agents` | 2 | same | `daalu agents` |
| `deploy/k8s/api/...` | `daalu-api` | 2+ | same | `daalu server` |

### New (PR 2)

`deploy/k8s/workers/executor.yaml`:

- `ServiceAccount: daalu-executor` — distinct from the rest. Lets cluster-level RBAC
  target the executor identity specifically.
- `Deployment: daalu-executor` — **single replica on purpose**. Adding pods creates
  racing executor identities. Scale throughput with `--concurrency=N` inside the
  process.
- `command: ["daalu", "executor", "--concurrency=2"]` — subscribes to the
  executor queue only.
- `env: EXECUTOR_JWT_SCOPE: "executor"` — pinned explicitly even though it
  matches the default. An ops secret rotation that omits this key should fail
  loudly, not silently rebind the gate.
- Resources: `100m / 256Mi` requests, `1 CPU / 768Mi` limits.

The image is the same `ghcr.io/kiwueke1/daalu-automation:latest`. Only the
command + queue subscription differ.

## Verifying the install

### Cross-check the identity boundary

This is the most important sanity check. Three independent layers must all
prove out:

```bash
# (a) Code gate — verify the executor's actor scope is enforced
kubectl exec -n daalu-automation deploy/daalu-api -- python -c "
from daalu_automation.core import change_proposals
from daalu_automation.core.sot.models import Actor
# This should raise PermissionError
import asyncio
async def go():
    try:
        await change_proposals.execute(
            db=None, tenant_id=None, proposal_id=None,
            actor=Actor(kind='engine', name='test'),
            sot=None, adapter=None, creds=None,
        )
        print('FAIL: should have raised PermissionError')
    except (PermissionError, AttributeError) as e:
        # PermissionError = good. AttributeError on db=None = bad (gate passed)
        if 'PermissionError' in type(e).__name__:
            print('OK: code gate enforced')
        else:
            print(f'FAIL: db check ran — gate bypassed. Got {type(e).__name__}')
asyncio.run(go())
"

# (b) Queue subscription — verify the main worker DOESN'T see the executor task
kubectl exec -n daalu-automation deploy/daalu-worker -- celery -A daalu_automation.workers.celery_app inspect active_queues | grep -c executor
# Expect 0.

kubectl exec -n daalu-automation deploy/daalu-executor -- celery -A daalu_automation.workers.celery_app inspect active_queues | grep -c executor
# Expect ≥ 1.

# (c) K8s identity — the executor SA exists and isn't shared
kubectl get sa -n daalu-automation daalu-executor
```

### End-to-end smoke (after at least one tenant has Nautobot wired)

```bash
# 1. Seed a fake device + intent in Nautobot.
# 2. Manually propose a change via the existing route:
curl -X POST https://daalu.example.com/api/v1/onboarding/test/nautobot \
  -H "Authorization: Bearer <token>" -d '{"config": {"url":"https://nautobot...","token":"..."}}'

# 3. After the reconciler tick, expect a drift proposal:
curl https://daalu.example.com/api/v1/change-proposals \
  -H "Authorization: Bearer <token>"

# 4. Approve it via the UI or:
curl -X POST https://daalu.example.com/api/v1/change-proposals/<id>/approve \
  -H "Authorization: Bearer <token>"

# 5. Within executor_period_s seconds, status flips to executed and
#    the executor_result has success: true.
curl https://daalu.example.com/api/v1/change-proposals/<id> \
  -H "Authorization: Bearer <token>"
```

## Secret rotation

Three secret rotations are operationally meaningful:

1. **The platform Anthropic API key.** Unchanged by this feature. Stored in
   `ANTHROPIC_API_KEY` env. Affects the LLM `propose_change` tool — if it
   rotates, the engine still works (failure is "AI copilot is offline").

2. **`MANAGED_NAUTOBOT_ADMIN_TOKEN` (hosted Nautobot only).** Used by the
   provisioning route to create per-tenant Tenants / Permissions / Tokens.
   Rotating doesn't affect existing tenants — their tokens are independent.
   To rotate:
   1. Mint a new admin token in Nautobot
   2. Update the daalu-automation secret
   3. Roll the API + worker + executor pods
   4. Optionally revoke the old admin token

3. **Per-tenant Nautobot tokens** stored in the tenant's `Integration` row.
   Rotation:
   - For BYO: tenant updates their own integration via the wizard
   - For hosted: re-run `POST /api/v1/onboarding/nautobot/provision` (idempotent
     on Tenant + Permission, mints a fresh token). The old token stays valid
     until you revoke it in Nautobot's admin UI.

## Database operations

### What lives in the new tables

Just `change_proposals` is new. It can grow large for chatty tenants — a few
operational levers:

- Approved + executed rows are kept forever for audit. If you need to prune,
  archive rows older than N months to cold storage and `DELETE` from the
  live table. **Be careful with `kind="drift"` rows** — they're operationally
  meaningful for change-frequency analysis.
- Pending rows older than the SoT intent's `proposed_at` timestamp by more
  than ~1 week are likely stale (the SoT intent has moved on). A future
  housekeeping task should auto-`mark_stale()` them.

### Backup considerations

- `change_proposals` is the audit log — back it up the same way as `events`,
  `alerts`, `cluster_tunnels`. Lose it and you lose the "who approved what
  when" history.
- The `intended_config` and `observed_config` columns are TEXT and can be
  several KB each. Don't be alarmed by row sizes.

## Monitoring this feature

Three things to watch:

1. **Drift detection rate.** `sum_over_time(events{type="proposal.created"}[1h])`
   grouped by `payload.kind=drift`. Spike = something changed reality without
   updating SoT. Often a human action (someone SSHed in); occasionally a
   compromised host.

2. **Stale proposal rate.** `sum_over_time(events{type="proposal.created"}[1h])`
   grouped by `status=stale` (need to also emit a `proposal.stale` event from
   `mark_stale()` for this to work; v1 doesn't but it's a small follow-up). High
   stale rate = SoT intent is changing faster than operators can approve.

3. **Executor lag.** `executor_result.started_at - approved_at` p50 and p99.
   Target p99 < `executor_period_s + 60s`. Higher = batch is backing up.
   Bump `executor_batch_size` or split into per-tenant queues.

Add a Grafana panel reading from your existing Prometheus scrape of the
daalu-api `/metrics` endpoint (not yet exposed for this feature — another
small follow-up).

## What to NOT touch in this feature

- **Do not run multiple `daalu-executor` replicas.** Single-replica is by
  design; multiple pods create racing executor identities on the same approved
  rows.
- **Do not route `sot.execute_approved` to the default queue.** That breaks
  the queue-subscription identity boundary (Layer 2 in the security model).
- **Do not give `daalu-worker` access to the `executor` queue.** Same reason.
- **Do not store the `EXECUTOR_JWT_SCOPE` env on any pod other than
  `daalu-executor`.** The pinned env is what makes "executor pod" identifiable.
- **Do not modify Nautobot's `ObjectPermission` constraints manually for
  hosted tenants.** Re-running `/api/v1/onboarding/nautobot/provision` is the
  supported path.

## Common operational scenarios

### Scenario: A tenant's reconciler isn't firing

Check, in order:
1. Does the tenant have both `nautobot` AND `ssh_credentials` Integration rows?
   The reconciler skips tenants missing either. `SELECT provider FROM
   integrations WHERE tenant_id = '...'`
2. Is the Nautobot URL reachable from the worker pod?
   `kubectl exec deploy/daalu-worker -- curl -I <nautobot url>/api/status/`
3. Is the token valid? Try the onboarding test:
   `POST /api/v1/onboarding/test/nautobot` with the same config
4. Are there devices? Drift detection on zero devices is a no-op.

### Scenario: A proposal won't execute

Check, in order:
1. Status — is it `approved`? `pending` won't execute; `failed` won't retry.
2. Is `daalu-executor` running? `kubectl get pods -n daalu-automation -l app=daalu-executor`
3. Is the device's transport recognized? Check `device.transport` in Nautobot.
   Unsupported transport → executor logs `executor.unknown_transport`.
4. Are credentials present? Missing `ssh_credentials` row for a tenant →
   executor logs `executor.creds_unresolved`.
5. Is `executor_result` showing stale? Then the SoT intent changed between
   approve and execute. Re-author or update intent to accept reality.

### Scenario: A push succeeded but observed didn't update

The adapter's `execute()` returned success but the next reconciler pass
still shows drift. Common causes:

- **Verification failed silently.** Add a `collect()` call right after
  `execute()` and assert the device state matches. (LinuxSSH does this
  implicitly via the snapshot+rollback dance; verify Redfish doesn't.)
- **Idempotent push that the device silently rejected.** Some Redfish BMCs
  return 200 on a PATCH but don't apply (e.g., BIOS attr requires a reboot).
  Read the docs for the specific BMC vendor.
- **A different reconciler tick beat the executor to a re-collect.** Race.
  Wait one more tick.

### Scenario: A pending proposal needs to be cancelled

Reject it: `POST /api/v1/change-proposals/<id>/reject`. Status flips to
`rejected`. The executor will not pick it up. The row stays for audit.
