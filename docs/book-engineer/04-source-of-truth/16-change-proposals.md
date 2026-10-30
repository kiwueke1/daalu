# 16. Change proposals — lifecycle and approval

Change proposals are the central safety primitive in Daalu.
Every state-changing operation goes through one. This chapter
covers the lifecycle, the database transitions, and the
approval gates.

Table: `change_proposals` (Chapter 6).

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
the workflow layer (Chapter 21 of the customer book) — the
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
