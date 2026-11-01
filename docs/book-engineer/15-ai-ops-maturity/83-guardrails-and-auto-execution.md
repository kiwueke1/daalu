# 83. Guardrails and automatic execution

Chapter 82 described how the policy decides. This chapter covers what
happens after an `auto` decision, how a rule earns `auto` from its track
record, the guardrails that narrow `auto` back to `approve`, and
multi-approver approval. This is the machinery of Levels 3 and 5.

Code: `core/aiops/policy.py` (trust, breaker, state),
`api/routers/alert_chat.py` (`_start_auto_actions`, `approve_action`,
`_run_approved_action`, `_record_outcome`), `api/routers/aiops_policy.py`
(kill switch, breaker reset, trust table).

---

## The path of an automatic action

```
model reply ── write tool call
   │
   ├─ validate_write_input ── error ─► bounced to the model
   ├─ decide_for_tenant ───── deny ──► bounced to the model, action.denied
   │                         approve ─► AlertAction pending (approve card)
   │                         auto ────► AlertAction approved, auto_executed=True
   │                                      action.proposed audit event
   ▼                                      id appended to auto_ids
turn loop ends, commit
   │
_start_auto_actions ── WorkflowRun "Auto-remediate: <alert>" (module=remediation)
   │                    asyncio task → _run_approved_action
   ▼
_run_approved_action ── execute the write verbatim
   │                    verification steps (wait for rollout, list pods)
   ▼
_record_outcome ─────── verified_resolved, investigation resolution
                        action.auto_executed + action.verified audit events
                        failed? → trip_breaker → policy.breaker_tripped
                        resolved? → maybe draft a runbook
```

* In the turn loop (`api/routers/alert_chat.py:1070`) an `auto` decision
  creates the `AlertAction` with `status=approved`, `auto_executed=True`,
  `approved_at=now`, and bumps the investigation's `actions_auto` counter.
  `approved_by` stays null: no human approved it, and the audit actor is
  `policy:<rule>` (`:1375`).
* `_record_assistant_turn` (`:873`) runs the whole loop first and only then
  calls `_start_auto_actions` (`:818`). Starting the write mid-loop would
  race the transcript: the model's write-up for the turn, and the commit
  that makes the action visible, must exist before anything runs.
* `_start_auto_actions` opens one `WorkflowRun` per action, commits, and
  starts `_run_approved_action` as an `asyncio` task held in `_AUTO_TASKS`
  (`:809`) so it isn't garbage-collected. `drain_auto_tasks` (`:812`) awaits
  them in tests.
* `_run_approved_action` (`:1405`) is the **same** function a human
  approval schedules (via FastAPI `BackgroundTasks`, `:1346`). There is no
  separate "auto executor". That is the core safety argument for `auto`: an
  automatic write gets exactly the execution, verification and re-engagement
  that a human-approved one gets.

We considered queueing auto actions to the Celery `executor` queue like SoT
change proposals (chapter 16). We kept them in-process because the alert
chat's writes already ran in-process via `BackgroundTasks` and were verified
there; moving only the automatic ones to Celery would have given the two
paths different failure semantics. The cost is that an in-flight automatic
action dies with the API pod; `_run_approved_action`'s `except` block
(`:1530`) marks the run and action failed when it can, but a pod kill
mid-execution leaves the run `running`. See "Hardening" below.

### Verification

`_verification_steps` (`:393`) and `_remediation_resolved` (`:417`) are
pre-AOMM: for deployment writes it waits up to 180 s for
`ready == available == desired` (`_wait_for_deployment`, `:442`), then lists
the namespace's pods. Any error, or a deployment not fully available, counts
as **not resolved**. The verdict is deliberately conservative: false
"unresolved" costs a re-investigation, false "resolved" would feed a bad
fix into the trust stats.

If not resolved, the run is marked failed and a user message asks the agent
to investigate again (`:1511`). The re-engagement goes through the policy
again like any other turn.

---

## `_record_outcome` — the feedback

`_record_outcome` (`api/routers/alert_chat.py:1361`) is where Level 3
feeds Level 4 and Level 5:

1. `action.verified_resolved = resolved` — the bit trust stats and the
   change-failure-rate metric count.
2. `investigations.note_outcome` — sets `resolution` to `auto` or `approved`
   and `resolved_at` on success, `unresolved` on failure of a still-open
   investigation (chapter 84).
3. Audit: `action.auto_executed` (actor `policy:<rule|defaults>`) or
   `action.executed` (actor `executor`), then `action.verified` (actor
   `verifier`).
4. **Breaker**: an `auto_executed` action that did not resolve calls
   `trip_breaker` (`core/aiops/policy.py:490`) and, if a breaker is
   configured, writes `policy.breaker_tripped`.
5. Commit, then, on success, `runbooks.maybe_draft_from_investigation`
   (chapter 85).

---

## Earning `auto`: trust stats

`trust_stats` (`core/aiops/policy.py:418`) counts, for one rule id, the
`AlertAction` rows in the last `window_days` with `policy_rule == rule_id`,
`executed_at` set and `verified_resolved` not null: true = success, false =
failure. `build_context` (`:464`) computes it only for the matching rule
and only when that rule is `auto_after_trust`.

Note what counts: **every executed action decided under that rule**,
human-approved ones included. That is the point. While a rule is still
earning, its writes are decided `approve` but stamped with
`policy_rule = <rule id>`, so each approved-and-verified fix is a success
for the rule. A team at Level 2 builds the track record that promotes the
rule; nobody has to "turn on" a probation mode.

### Why trust is per rule

The alternatives were trust per tool (`scale_deployment` has earned it
everywhere) or per tenant (the AI has earned it). Per tool is wrong because
the same tool has very different risk in `dev-*` and `prod-*`; per tenant is
wrong because one flawless kind of fix would promote every kind. A rule is
the unit a human wrote to say "these writes are alike", so it is the unit
whose record should speak for them. The consequence: renaming a rule's `id`
resets its track record, and two rules with the same match but different ids
earn separately.

`GET /aiops/trust` (`api/routers/aiops_policy.py:213`) returns one row per
rule of the active policy: mode, tools, successes, failures, failure rate,
thresholds (for `auto_after_trust`), `earned`, and `breaker_until`. The
Autonomy page shows it as "Trust by rule".

---

## Guardrails

Every guardrail only turns an `auto` decision into `approve`. None can
create an `auto`, and none apply to `deny`.

### Kill switch

`POST /aiops/policy/kill-switch {enabled}` (`api/routers/aiops_policy.py:169`)
sets `aiops_tenant_state.kill_switch`, records who and when, and writes
`policy.kill_switch`. While on, every would-be `auto` write is decided
`approve` with the reason "kill switch is on: all automatic actions paused".
It does not cancel automatic actions already running.

**Anyone** in the tenant may turn it on; only an admin may turn it off.
Stopping automation is the action you want the most junior on-call engineer
to take without asking, at 3 a.m.; resuming it is a decision about the
policy and belongs to whoever owns the policy. The design doc says kill
switch writes require an admin; the code deliberately relaxes that for "on".

### Circuit breaker

`trip_breaker` stores `now + cooldown_minutes` under the rule id (or
`__defaults__` when the defaults decided) in `aiops_tenant_state.breaker`.
`decide` demotes that rule to `approve` until the time passes
(`core/aiops/policy.py:335`). With `circuit_breaker: null` nothing trips.

Only automatic actions trip it. A human-approved fix that fails
verification is a judgement shared by the approver; demoting the rule for it
would punish the rule for a decision the policy didn't make. It still counts
as a failure in the trust stats.

Reset: the breaker expires on its own, or an admin calls
`POST /aiops/policy/breaker/reset?rule_id=<id>` (empty `rule_id` resets
`__defaults__`), which writes a `policy.updated` event with subject
`breaker-reset`. The UI shows a **Reset** button in the Trust table and a
banner for a tripped defaults breaker.

### Freeze windows

`in_freeze_window` (`core/aiops/policy.py:281`) converts `now` into each
window's `tz`, checks the weekday, and compares `HH:MM` strings. A window
whose `start > end` crosses midnight (`22:00`–`06:00`). Windows are
evaluated per write, at decision time; a write decided `auto` one minute
before a freeze still runs.

### Hourly cap

`auto_actions_since` (`:446`) counts the tenant's `auto_executed` actions
created in the last hour; at or above `max_auto_actions_per_hour` the
decision is `approve`. The cap is tenant-wide, not per rule: it bounds the
total blast rate of automation, which is the property an incident commander
cares about.

### External agents

An MCP proposal (`WriteRequest.external=True`) is decided `approve` unless
the matching rule sets `allow_external_auto: true` (chapter 90).

---

## Multi-approver

A rule's `approvers: N` is copied to `AlertAction.approvals_required`.
`approve_action` (`api/routers/alert_chat.py:1262`):

* appends `{user_id, email, at}` to `action.approvals`; the same user twice
  is a 409 (`:1295`);
* below `N`, writes `action.approval_recorded` and returns
  `workflow_run_id: ""` with `approvals_remaining` (`ApproveActionOut`,
  `:501`); the action stays `pending`;
* at `N`, marks the action approved, bumps `actions_approved`, writes
  `action.approved` with every approver's email, and starts the run.

The UI shows an "n / N approvals" chip and an "Add my approval" button
while more than one approval is still missing, and "Your approval is
recorded, waiting for N more" after a partial approval
(`frontend/components/aiops/action-policy.tsx`,
`frontend/components/alerts/investigation-tabs.tsx`).

---

## Operational notes

* **Pause everything now**: Autonomy page → **Pause all automation**, or
  `curl -X POST $DAALU/api/v1/aiops/policy/kill-switch -H "Authorization: Bearer $TOKEN"
  -H 'Content-Type: application/json' -d '{"enabled":true}'` with any user's token.
* **A rule keeps tripping**: the Investigations page filtered to
  "Unresolved" lists the failed fixes; narrow the rule's `match`, switch it
  to `auto_after_trust`, or to `approve`, then reset the breaker.
* **Nothing runs automatically although the rule is `auto`**: run the
  simulator. The reason string names the guardrail (kill switch, breaker,
  freeze, cap, external) or the trust shortfall.
* **In-flight automatic actions at shutdown**: they are plain asyncio tasks
  in the API process. A rolling restart can cut one off mid-verification.

---

## Hardening (fixed before release)

A review of the first implementation found four ways the guardrails could be
bypassed or mis-counted. Each is fixed and has a regression test in
`tests/test_aiops_flow.py`:

* **The hourly cap counts writes decided in the same turn.** All writes in a
  model turn are decided before any is inserted, so `decide_for_tenant`
  takes `pending_auto` (the number of writes already decided `auto` this
  turn) and adds it to the committed count
  (`test_hourly_cap_counts_writes_in_the_same_turn`).
* **Approvals are serialised.** `approve_action` loads the action with
  `SELECT … FOR UPDATE`, so two approvers clicking at once can't lose a vote
  or start the action twice.
* **A crashed execution counts as a failure.** The `except` block in
  `_run_approved_action` now calls `_record_outcome(..., resolved=False)`, so
  a crash trips the breaker and counts against the rule's trust like any
  failed verification (`test_crash_during_auto_fix_counts_as_failure`).
* **The prompts match the policy.** `SYSTEM_PROMPT` says writes are gated by
  the autonomy policy (approve by default; denied or automatic where the
  policy says), and the re-engagement message after a failed verification
  says the next fix "goes through the autonomy policy".

Still true: an automatic action in flight dies with the API pod (the run is
left `running`), and with no `circuit_breaker` configured the only bound on
repeated failing automatic fixes is the hourly cap. Configure both guardrails
on any policy with `auto` rules; the Level 5 criterion requires them.

---

## Testing

* `tests/test_aiops_flow.py` — end to end through the real turn loop with a
  scripted model: `test_auto_policy_runs_and_verifies_without_a_human`,
  `test_failed_auto_fix_trips_breaker`, `test_two_approvers_required`,
  `test_hourly_cap_counts_writes_in_the_same_turn`,
  `test_crash_during_auto_fix_counts_as_failure`,
  `test_mcp_proposer_cannot_approve_own_action`.
* `tests/test_aiops_policy.py` — `test_auto_after_trust_thresholds`,
  `test_guardrails_demote_auto`, `test_freeze_window_crossing_midnight`.
