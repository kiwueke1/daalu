# 16. Change proposals — lifecycle and approval

Change proposals are the central safety primitive in Daalu.
Every state-changing operation goes through one. This chapter
covers the lifecycle, the database transitions, and the
approval gates.

Table: `change_proposals` (Chapter 6).

> **Scope.** "Every state-changing operation" is true for the **source-of-truth
> and device pipeline** only. Kubernetes remediation proposed from an alert's
> chat does **not** create a change proposal; it uses a separate, lighter gate
> (pending `AlertAction` rows) that is now governed by the autonomy policy. The
> next section explains both paths and when each applies.

---

## Two approval paths: change proposals vs alert actions

Daalu has **two** approval gates, backed by different tables, different
endpoints and different executors. Older chapters (and older demo scripts) call
both of them "change proposals"; only one is.

| | **Change proposal** (this chapter) | **Alert action** |
|---|---|---|
| What it changes | Device config / server lifecycle in the SoT pipeline (NV-CM network plane, Tinkerbell server plane) | A Kubernetes workload (or a non-GET `call_external_api`) in the alert's cluster |
| Model / table | `ChangeProposal`, `change_proposals` (`models/change_proposal.py:75`) | `AlertAction`, `alert_actions` (`models/alert_chat.py:78-140`) |
| Who authors it | the reconciler on drift (`workers/reconciler.py:212`), a drift check from the device page (`api/routers/sot_devices.py:1099`), or the agent's `propose_change` tool (`core/sot_tools.py:158`) | the alert-chat agent calling a write tool (`kube_tools._WRITE_TOOLS`: `rollout_undo`, `scale_deployment`, `restart_deployment`, `delete_pod`, `patch_resource` — `core/kube_tools.py:1327`), or an external agent via MCP `propose_action` (`core/aiops/mcp.py`, `actor="mcp:<email>"`) |
| Statuses | `pending → approved → executed`, or `rejected` / `failed` / `stale` (`models/change_proposal.py:66-72`) | `pending → approved → executed`, or `rejected` / `failed` (`models/alert_chat.py:70-75`) |
| Approve / reject | `POST /change-proposals/{id}/approve` · `/reject` (`api/routers/change_proposals.py:89-118`) | `POST /alerts/{alert_id}/actions/{action_id}/approve` · `/reject` (`api/routers/alert_chat.py:1258` and `1545`) |
| Where it shows | Operations → Proposals | the Approve / Reject card in the alert's chat |
| Who executes | the **executor** service: Celery task `sot.execute_approved` on its own queue and identity (`workers/executor.py:271`, chapter 17), which re-renders from the SoT and flips the row to `stale` if the intent moved | the **API process**: `_run_approved_action` (`alert_chat.py:1405`) as a FastAPI background task, running `kube_tools.execute_tool` with the tenant's kubeconfig |
| After execution | executed / failed | **verification**: wait for the rollout, check pods, judge `verified_resolved` (§ below) |
| Autonomy policy | **not consulted** | **every write is decided by it** (deny / approve / auto, approver count) |

### How an alert action is gated

1. **Classification.** In the assistant turn loop, any tool call for which
   `kube_tools.tool_requires_approval` is true is a write
   (`alert_chat.py:961`). Its arguments are validated first
   (`validate_write_input`); invalid writes are bounced back to the model and
   never become rows.
2. **Policy decision** (`alert_chat.py:969-990`): `policy.decide_for_tenant`
   evaluates the tenant's autonomy policy for
   `(tool, cluster, namespace, severity)` and returns a `Decision` with a
   `mode` and an `approvers` count (`core/aiops/policy.py:245`, `482`).
   - **deny** → no row at all; the model is told the action was denied and to
     propose something else, and `action.denied` is audited.
   - **approve** → an `AlertAction` in `pending`, carrying `policy_mode`,
     `policy_rule`, `policy_reason`, `approvals_required` and a dry-run
     `preview` (`alert_chat.py:1049-1074`).
   - **auto** → the same row, created already `approved` with
     `auto_executed=True`, and run immediately through the same path a human
     approval uses (`_start_auto_actions`, `alert_chat.py:818`).
3. **Approval** (`approve_action`, `alert_chat.py:1258-1358`): requires the row to
   be `pending` (409 otherwise). Each approval is recorded as a vote in
   `approvals`; the same user can't vote twice. Until `approvals_required`
   distinct users have approved, the call returns `approvals_remaining` and
   nothing runs. On the last vote the row flips to `approved`, a
   `WorkflowRun` ("Remediate: <alert title>") is opened, and execution starts in
   the background; the response returns the run id so the UI can open it.
4. **Execution and verification** (`_run_approved_action`,
   `alert_chat.py:1405-1542`): the approved tool call runs **exactly as
   approved**, then read-only verification steps run
   (`_verification_steps`, line 393: for a deployment write, wait for the rollout
   and check `desired/ready/available`; then list the namespace's pods).
   `_remediation_resolved` (line 417) is deliberately conservative — any error or
   an under-available deployment counts as unresolved.
5. **Outcome** (`_record_outcome`, line 1361): stores `verified_resolved`,
   audits `action.executed` / `action.auto_executed` and `action.verified`, trips
   the policy's **circuit breaker** (when the policy configures one) if an
   *automatic* action didn't fix things (`policy.trip_breaker`), and drafts a runbook from a verified fix. If the
   action didn't resolve the alert, the agent is re-engaged — and anything it
   proposes next goes through the same gate again.
6. **Reject** (`alert_chat.py:1545`) marks the row `rejected`, writes a tool
   message "rejected by <email>: tool … not executed" so the model sees it, and
   runs another assistant turn.

Writes proposed by an **external agent over MCP** go through the same policy
with `external=True`; a rule's `auto` drops to `approve` for them unless the rule
sets `allow_external_auto` (`core/aiops/policy.py:332-334`).

### When each applies

- The alert-chat agent wants to roll back, scale, restart or patch a Kubernetes
  workload → **alert action**. This is the gate the "alert to approved fix" demo
  shows.
- Anything that changes a device's intended config, a drift remediation, or a
  Tinkerbell provision/power operation → **change proposal**, executed by the
  executor identity described in chapter 17.
- The agent can author a change proposal *from* an alert investigation (its
  `propose_change` tool). That is still a change proposal: it lands on the
  Proposals page, not as an approve card in the chat, and the autonomy policy
  does not gate it.

The autonomy policy — its rule format, modes, approver counts, trust stats and
circuit breaker — is chapter
[82 · Autonomy policy](../15-ai-ops-maturity/82-autonomy-policy.md).

> **Note on the rest of this chapter.** The state diagram and the
> `approve_proposal` excerpt below predate the current code. The live enum is
> `pending / approved / rejected / executed / failed / stale`
> (`models/change_proposal.py:66-72`), and the live approve path
> (`api/routers/change_proposals.py:89` → `core/change_proposals.py:166`) checks
> tenant ownership and `pending` status but does not compare the approver with
> the author. Trust the code where they differ.

---

## States

A proposal moves through these states:

```
   proposed ─┬─► approved ─► applying ─┬─► applied
             │                          │
             │                          └─► error
             │
             └─► denied
```

Status field values:

- `proposed` — written by an author (user, agent, or
  workflow). Awaiting approval.
- `approved` — a human approver has signed off. Queued for
  execution.
- `applying` — executor has picked it up; in flight.
- `applied` — execution succeeded.
- `error` — execution failed.
- `denied` — the proposal was rejected without execution.

Transitions are recorded in the proposal's audit log column
with the actor, timestamp, and reason if provided.

---

## Author identities

A proposal's `proposed_by` is either:

- `proposed_by_user_id` — a user authored it directly
  (manual proposal, AI proposal where the user asked).
- `proposed_by_agent` — a string like `reconciler`,
  `alert-explainer`, `workflow:<id>`.

Both can't be set simultaneously.

---

## The four-eyes rule

Enforced in `api/routers/proposals.py:approve_proposal`:

```python
@router.post("/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    proposal = await db.get(ChangeProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404)
    if proposal.tenant_id != user.tenant_id:
        raise HTTPException(404)   # don't leak existence
    if proposal.status != ChangeProposalStatus.PROPOSED:
        raise HTTPException(409, "proposal not in proposed state")
    if proposal.proposed_by_user_id == user.id:
        raise HTTPException(403, "self-approval not allowed")
    proposal.status = ChangeProposalStatus.APPROVED
    proposal.approved_by_user_id = user.id
    proposal.approved_at = datetime.now(tz=timezone.utc)
    await db.commit()
    await queue_executor(proposal.id)
    return {"status": "approved"}
```

The self-approval check is the four-eyes rule. It applies to
*user* authorship — agents are not users, so an agent-authored
proposal can be approved by any user (subject to permission).

---

## Approval permissions

Today: any user in the tenant can approve any proposal they
didn't author. Admins included.

If you wanted stricter rules (specific approvers, two
approvers, ticket integration), the place to enforce them is
the workflow layer (Chapter 21 of the user guide) — the
proposal API itself stays simple.

---

## Execution

When a proposal is approved, the approve handler queues an
executor task:

```python
async def queue_executor(proposal_id: UUID):
    await celery_app.send_task(
        "daalu.sot.execute_approved",
        args=[str(proposal_id)],
        queue="executor",
    )
```

The task lands in the `executor` queue, which only
**daalu-executor** subscribes to.

The executor's main loop:

```python
@celery_app.task(name="daalu.sot.execute_approved", bind=True)
def execute_approved(self, proposal_id: str):
    with db_session() as db:
        p = db.get(ChangeProposal, UUID(proposal_id))
        if p.status != ChangeProposalStatus.APPROVED:
            return    # raced, already handled

        p.status = ChangeProposalStatus.APPLYING
        db.commit()

        try:
            result = run_plan(p.plan, tenant=p.tenant_id)
            p.status = ChangeProposalStatus.APPLIED
            p.result = result
            p.applied_at = datetime.now(timezone.utc)
        except Exception as exc:
            p.status = ChangeProposalStatus.ERROR
            p.result = {"error": str(exc)}
        db.commit()
```

`run_plan` dispatches to the right adapter based on
`plan.action`. The executor's identity (JWT scope `executor`)
is verified inside `run_plan` for write tools — see Chapter
17.

---

## Idempotency

The executor uses the proposal ID as an idempotency token
where the external system supports it:

- AWS API calls — `ClientRequestToken` set to a hash of the
  proposal ID.
- Stripe — `Idempotency-Key` header.
- Network devices — most don't have idempotency primitives;
  we rely on the apply-config flow being idempotent by
  nature (same config → same result).

If the executor restarts mid-execution, the next attempt
sees the proposal already in `applying` and bails:

```python
if p.status != ChangeProposalStatus.APPROVED:
    return
```

This means rare cases get stuck in `applying`. A separate
sweep task runs hourly and rescues them (transitions back to
`approved` with a note).

---

## Audit log

Every state transition writes to `audit_logs`:

```python
async def write_audit(db, proposal, actor, action, detail):
    db.add(AuditLog(
        tenant_id=proposal.tenant_id,
        actor_user_id=actor.id if isinstance(actor, User) else None,
        actor_agent=actor if isinstance(actor, str) else None,
        target_kind="change_proposal",
        target_id=proposal.id,
        action=action,
        detail=detail,
    ))
```

`actor` is either a User or a string agent name. The audit
table is append-only and never edited.

---

## Reverting an applied proposal

Currently: no formal revert. The right pattern is to author a
new proposal that undoes the change.

A future feature: every applied proposal stores a `revert_
plan` field that the executor can run to undo. Not shipped
yet.

---

## Permissions on read

A proposal is readable by any user in its tenant. Including
its diff, plan, audit log. We've considered hiding plans
behind a "show details" gate but the audit trail is more
useful when everyone can see what's in flight.

---

## What's next

Chapter 17 — the executor identity and the JWT-scope gate
that backs it.
