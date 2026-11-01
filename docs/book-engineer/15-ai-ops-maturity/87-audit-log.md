# 87. The audit log

Everything the AI and its approvers do (reads, proposals, denials,
approvals, executions, verifications, policy changes, verdicts, runbook
publications, MCP calls, eval runs) is written to an append-only,
per-tenant, hash-chained log. Editing or deleting any event breaks every
hash after it. The audit chain is a Level 5 criterion
(`audit_chain_ok`): you can't hand routine incidents to an AI without a
record a security team can verify.

Code: `core/aiops/audit.py`, `api/routers/aiops_audit.py`.

---

## Data model

Table `audit_events`, model `AuditEvent` (`models/aiops.py:101`):

| Column | Meaning |
|---|---|
| `seq` | Per-tenant, monotonic from 1, unique `(tenant_id, seq)` |
| `kind` | One of `KINDS` (below) |
| `actor` | Who: a user's email, `agent`, `executor`, `verifier`, `policy:<rule or defaults>`, `mcp:<email>` (truncated to 255) |
| `subject` | What it is about: usually the alert id; `v<n>` for a policy version, `on`/`off` for the kill switch, a rule id, a runbook slug, an MCP tool name |
| `data` | JSON details |
| `prev_hash` | The previous event's `hash` (`""` for `seq` 1) |
| `hash` | `sha256(prev_hash + canonical_json(body))` |

The body is `{seq, kind, actor, subject, data, created_at}` with
`created_at` in UTC ISO format (`_body`, `core/aiops/audit.py:58`);
canonical JSON is `json.dumps(sort_keys=True, separators=(",", ":"))`
(`_canonical`, `:50`). `tenant_id` and the row `id` are not hashed: the
chain is per tenant, so the tenant is implied, and the id carries no
meaning.

---

## Writing: `record`

```python
await audit.record(db, tenant_id, "action.approved", actor=user.email,
                   subject=str(alert.id), data={...})
```

`record` (`core/aiops/audit.py:69`), inside a savepoint
(`db.begin_nested()`):

1. `SELECT pg_advisory_xact_lock(hashtext('audit:<tenant_id>'))`;
2. read the tenant's last `(seq, hash)`;
3. insert `seq + 1` with `prev_hash` and the computed `hash`.

The event commits with the caller's transaction, together with the change
it describes. A rolled-back approval leaves no `action.approved` event.

### Why a per-tenant advisory lock

Two writers for the same tenant (an approval request and a background
verification, say) would otherwise both read `seq = 41` and both insert
`seq = 42` with the same `prev_hash`: a forked chain, and one of them fails
the unique constraint and takes its business transaction down with it.
Alternatives we rejected:

* **`SELECT ... FOR UPDATE` on the last event** locks nothing when the
  tenant has no events yet, so the first two writers still race.
* **A Postgres sequence** gives unique `seq` values but not "the previous
  hash", and leaves gaps on rollback, which verification would report as
  tampering.
* **A table-wide lock** serialises every tenant behind the busiest one.

`pg_advisory_xact_lock` keyed on the tenant serialises exactly the writers
that share a chain, is released automatically at commit or rollback, and
needs no extra table. The key is `hashtext('audit:' || tenant_id)`; the
`audit:` prefix keeps it from colliding with any other advisory-lock user
hashing a bare tenant id. The lock is held until the *outer* transaction
ends, so keep audit writes near the end of long transactions.

### Why `record` never raises

An audit write failing must not turn a successful remediation into a 500 or
roll back an approval. `record` catches everything, logs
`aiops.audit.record_failed`, and returns `None`; the savepoint keeps a
failed insert from poisoning the caller's transaction. The trade-off is that
an event can be missing without a gap in `seq` (the failed insert never
claimed a number). Watch for that log line.

---

## Kinds

`KINDS` (`core/aiops/audit.py:29`). The same list is the enum in
`spec/audit-event.schema.json`, which also allows `x-` prefixed extension
kinds.

| Kind | Written by | Actor | Subject |
|---|---|---|---|
| `tool.read` | agent loop, every read tool call (`api/routers/alert_chat.py:1103`) | `agent` | alert id |
| `action.proposed` | agent loop (`:1080`), MCP `propose_action` | `agent` / `mcp:<email>` | alert id |
| `action.denied` | agent loop (`:983`), MCP | `agent` / `mcp:<email>` | alert id |
| `action.approval_recorded` | `approve_action` below the approver count (`:1302`) | approver email | alert id |
| `action.approved` | `approve_action` at the count (`:1321`) | last approver's email; `data.approvers` lists all | alert id |
| `action.rejected` | `reject_action` (`:1576`) | rejecter email | alert id |
| `action.executed` | `_record_outcome`, human-approved | `executor` | alert id |
| `action.auto_executed` | `_record_outcome`, automatic | `policy:<rule or defaults>` | alert id |
| `action.verified` | `_record_outcome` | `verifier` | alert id |
| `policy.updated` | `PUT /aiops/policy` (full YAML in `data`); breaker reset | admin email | `v<n>` / `breaker-reset` |
| `policy.kill_switch` | `POST /aiops/policy/kill-switch` | user email | `on` / `off` |
| `policy.breaker_tripped` | `_record_outcome` | `verifier` | rule id or `defaults` |
| `investigation.verdict` | `POST .../verdict`, MCP `submit_verdict` | user email | alert id |
| `runbook.published` | publish, or `PUT` of a published runbook | user email | slug |
| `runbook.drafted` | `maybe_draft_from_investigation` | `agent` | alert id |
| `mcp.call` | every MCP `tools/call` (`core/aiops/mcp.py:855`) | user email (no `mcp:` prefix) | tool name |
| `eval.recorded` | `POST /aiops/evals/runs` | user email | alert id or scenario id |

The design doc's version of this list matches; it does not specify actors.
Note the `mcp.call` actor is the bare email while MCP-originated
`action.*` events use `mcp:<email>`; filter on both when tracing an external
agent.

---

## Verifying: `verify_chain`

`verify_chain` (`core/aiops/audit.py:119`) walks the tenant's events in
`seq` order and returns `{ok, checked, broken_at, reason}` at the first of:

* a sequence gap (`expected N, found M`): a deleted row;
* `prev_hash` not equal to the previous event's hash: a deleted or
  reordered row, or an edited hash;
* the recomputed hash not equal to the stored one: edited content.

### What it does and doesn't prove

The chain is **tamper-evident, not tamper-proof**. It detects a change to,
or removal of, any event that has a later event after it. It does not
detect:

* deletion of the newest events (the chain is simply shorter);
* someone with database write access rewriting the whole chain from the
  edit point onward with recomputed hashes.

Nothing at the database level stops `UPDATE` or `DELETE` on `audit_events`;
the application never issues them. Closing either gap needs an anchor
outside the database (periodically publishing the latest `(seq, hash)` to
an external store or the customer's SIEM). That is the obvious next step
and is not built.

`metrics.collect` calls `verify_chain` on every `/aiops/metrics` and
`/aiops/maturity` request (`core/aiops/metrics.py:174`), which loads the
whole chain. Fine at today's volumes; at a few hundred thousand events per
tenant it will need a checkpoint (verify from the last verified `seq`).

---

## API

`api/routers/aiops_audit.py`:

| Method | Path | Line | Notes |
|---|---|---|---|
| GET | `/aiops/audit?kind=&subject=&actor=&before_seq=&limit=` | `:34` | Newest first, ≤ 1000. `kind` accepts `*` wildcards (`action.*`); `subject` and `actor` are exact. Page with `before_seq`. |
| GET | `/aiops/audit/verify` | `:64` | `{ok, checked, broken_at, reason}` |
| GET | `/aiops/audit/kinds` | `:72` | The `KINDS` list, for the UI filter. Not in the design doc's API table. |

Any authenticated user of the tenant can read the log. The UI is
`frontend/app/audit/page.tsx`: filters, a **Verify chain** button, and
expandable rows with the JSON `data` and both hashes.

---

## Operational notes

* **"Chain broken at seq N"**: treat it as a security event. Compare the
  row at `seq N` against database backups; the `reason` says whether it was
  a gap, a broken link or edited content. Do not "repair" the chain by
  recomputing hashes: that destroys the evidence and is exactly the attack
  the chain exists to reveal.
* **Missing events with no gap**: search the API logs for
  `aiops.audit.record_failed`.
* **Retention**: none. The table grows forever. Deleting old events breaks
  verification by design; if retention becomes a requirement it needs a
  signed checkpoint scheme first.

---

## Testing

* `tests/test_aiops_flow.py::test_verdict_and_tamper_detection` — edits an
  event's `data` and a hash in the database and asserts `verify_chain`
  reports the broken `seq`.
* `tests/test_aiops_mcp.py::test_list_alerts_and_audit`,
  `::test_audit_arguments_are_truncated` — `mcp.call` events.
* `tests/test_specs.py::test_audit_kind_pattern` — the spec's kind pattern
  matches `KINDS`.
