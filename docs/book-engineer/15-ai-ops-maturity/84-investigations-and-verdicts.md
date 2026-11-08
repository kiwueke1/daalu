# 84. Investigations and verdicts

An **investigation** is the learning-loop record of one AI investigation of
one alert: when it started, when the AI named a root cause and what it was,
what it proposed and what happened to each proposal, how the alert was
resolved, and whether a human agreed with the diagnosis. It is the row that
turns a chat transcript into metrics (chapter 88). It serves Level 1 (a
shared, recorded investigation instead of a private chatbot session) and
Level 4 (verdicts, verified outcomes).

Code: `core/aiops/investigations.py`, `api/routers/aiops_investigations.py`,
the hooks in `api/routers/alert_chat.py`, `modules/infra/agent.py`,
`api/routers/alerts.py`.

---

## Data model

Table `investigations`, model `Investigation` (`models/aiops.py:71`). One
row per alert: `alert_id` is unique and cascades on alert delete.

| Column | Meaning |
|---|---|
| `started_at` | When the record was created (first triage turn) |
| `triaged_at`, `root_cause` | Set once, from the first assistant message containing a root-cause section |
| `actions_proposed`, `actions_approved`, `actions_rejected`, `actions_auto` | Counters bumped as writes move |
| `resolution` | `open` (default) · `auto` · `approved` · `manual` · `unresolved` |
| `resolved_at`, `verified` | Outcome of the last verified write |
| `runbook_ids` | Published runbooks injected into this triage (chapter 85) |
| `verdict`, `verdict_by`, `verdict_at`, `verdict_note` | Human grade: `correct` · `partial` · `wrong` |
| `model` | The model that first answered, from the LLM reply |

---

## How the record is kept current

All the writes happen in the alert-chat code; `core/aiops/investigations.py`
is a small set of pure-ish helpers.

| Event | Where | Helper |
|---|---|---|
| A triage turn starts | `_assistant_turn_loop`, `api/routers/alert_chat.py:909` | `ensure` (`investigations.py:49`) |
| Every assistant message | `api/routers/alert_chat.py:1003` | `note_answer` (`:65`) |
| A write is queued | `:1074`–`:1075` | `bump("actions_proposed")`, plus `bump("actions_auto")` for `auto` |
| A write reaches its approver count | `approve_action`, `:1319` | `bump("actions_approved")` |
| A write is rejected | `reject_action`, `:1574` | `bump("actions_rejected")` |
| A write is verified | `_record_outcome`, `:1371` | `note_outcome` (`:80`) |
| An MCP agent proposes | `core/aiops/mcp.py:470` | `ensure` + `bump` |

### Root-cause detection

`note_answer` sets `triaged_at` and `root_cause` the first time
`extract_root_cause` (`investigations.py:34`) finds a root-cause section.
The regex (`_ROOT_CAUSE_RE`, `:27`) is lenient about form, because models
are: a markdown heading of any level, a bold `**Root cause**`, or a
`Root cause:` line, case-insensitive. The section runs until a
`Background` or `Remediation plan` heading (the alert chat's three-section
answer format) or the end of the message, capped at 4000 characters.

`triaged_at` is set once and never moved. A later, better diagnosis
doesn't reset time-to-triage; it is the time to the *first* stated root
cause, which is the number on-call actually experiences. The `model` is
likewise the first model that answered.

### Resolution

`note_outcome` (`:80`):

* verified resolved → `resolution = auto` if the action was auto-executed,
  else `approved`; `resolved_at = now`; `verified = True`;
* not resolved → `verified = False`, and `resolution = unresolved` only if it
  was still `open` (a later failed fix doesn't erase an earlier success).

**Manual** resolution is set from two places, both only when the
investigation is still `open`:

* a human clicks Resolve: `resolve_alert`, `api/routers/alerts.py:112`;
* the monitoring signal clears and the infra agent auto-resolves the alert:
  `mark_manual_resolution`, `modules/infra/agent.py:60`, called from
  `_auto_resolve_alert`.

Both run in a savepoint and swallow errors: an AOMM bookkeeping failure must
never block resolving an alert. `metrics.effective_resolution`
(`core/aiops/metrics.py:47`) applies the same rule at read time, so
alerts resolved before this code shipped, or by any other path, still count
as `manual`.

"Manual" therefore means "resolved without a verified fix through Daalu",
which includes a human fixing it by hand and the problem going away on its
own. We considered distinguishing the two and didn't: Daalu can't tell them
apart reliably, and for the metric that matters (what share was resolved
*automatically*), both are "not by the AI".

---

## Verdicts

```
POST /api/v1/aiops/investigations/{id}/verdict   {verdict: correct|partial|wrong, note}
```

`submit_verdict` (`api/routers/aiops_investigations.py:187`) calls
`set_verdict` (`investigations.py:89`) and writes an
`investigation.verdict` audit event. Any user may grade; a later verdict
overwrites the earlier one (the audit log keeps both). The MCP
`submit_verdict` tool calls the same function with the token's user.

Verdicts are the ground truth for `agreement_rate`
(`(correct + 0.5 · partial) / verdicts`) and `verdict_coverage`
(chapter 88). Partial counts half because "right component, wrong cause" is
common and worth something; counting it as wrong would make the metric
punish the model for the most useful kind of miss.

Why a three-way human verdict rather than inferring correctness from
whether the fix worked? Because the two diverge in both directions: a
restart can "fix" a memory leak whose diagnosis was wrong, and a correct
diagnosis can come with a fix the policy denied. Verification measures the
change; the verdict measures the reasoning. AOMM tracks both.

---

## Automatic investigation

With `investigate_automatically: true` in the saved policy, the infra
agent starts the alert-chat triage on every new alert instead of waiting for
someone to open it:

* `InfraAgent` emits the alert, then calls `_maybe_auto_investigate`
  (`modules/infra/agent.py:36`, called at `:199`). It loads the policy and, if
  the flag is set, starts `alert_chat.auto_investigate` as an asyncio task
  (held in `_AUTO_INVESTIGATIONS`).
* `auto_investigate` (`api/routers/alert_chat.py:1222`) does what
  `POST /alerts/{id}/triage` does without HTTP: it is a no-op if the alert
  already has an assistant message, otherwise it appends the triage kickoff
  message and runs `_record_assistant_turn`. It never raises.

This is the Level 1 `triage_rate ≥ 0.5` lever. The flag lives in the policy
rather than a tenant setting because it is an autonomy decision (spending
model tokens and reading production state without a human asking) and
should be reviewed and versioned with the rest of the policy.

---

## API

| Method | Path | Line | Notes |
|---|---|---|---|
| GET | `/aiops/investigations?days=&resolution=&needs_verdict=&limit=` | `aiops_investigations.py:95` | Newest first; `needs_verdict` = triaged and no verdict. Each row carries the alert title/severity/status and computed `time_to_triage_s`, `time_to_resolve_s` (both from `alert.created_at`). |
| GET | `/aiops/investigations/{id}` | `:158` | Plus `actions`: every write action on the alert with policy mode/rule, `auto_executed`, `verified_resolved`, approvals, actor. |
| GET | `/aiops/investigations/by-alert/{alert_id}` | `:136` | Same, keyed by alert. Used by the alert page panel, the eval runner and MCP. Not in the design doc's API table. |
| POST | `/aiops/investigations/{id}/verdict` | `:187` | See above. |

---

## UI

* `/investigations` (`frontend/app/investigations/page.tsx`): window,
  resolution filter, "Needs a verdict"; columns Alert, Started, To triage,
  Actions P / A / R / Auto, Resolution, Verdict. `?open=<id>` opens the
  drawer (`frontend/components/aiops/investigation-drawer.tsx`) with the root
  cause, verdict buttons and actions.
* Alert page, Root cause tab: `AlertInvestigationPanel`
  (`frontend/components/aiops/investigation-panel.tsx`) with the resolution,
  time to triage/resolve, action counts, runbooks used, and the verdict
  buttons (`frontend/components/aiops/verdict.tsx`).

---

## Failure modes

| Symptom | Cause |
|---|---|
| "No investigation record yet" on an alert | Nobody opened it and `investigate_automatically` is off, or auto-investigation failed (`alert_chat.auto_investigate_failed` in the API logs). |
| `triaged_at` null although the chat clearly explains the cause | The model never wrote a root-cause heading or `Root cause:` line. Re-triage; the alert chat's summary nudge asks for the three-section answer. |
| Resolution `manual` though Daalu ran the fix | The alert cleared before verification finished; `note_outcome` overwrites `manual` with `approved`/`auto` once verification reports success. |
| Resolution stays `unresolved` after a later fix worked | It doesn't: a later verified success sets `approved`/`auto`. A later *failure* never overwrites a success. |

---

## Testing

* `tests/test_aiops_flow.py::test_verdict_and_tamper_detection` — verdict
  through the API plus the audit event.
* `tests/test_aiops_runbooks.py::test_auto_investigate_is_idempotent`,
  `::test_agent_host_auto_investigates_only_when_policy_says_so`,
  `::test_resolving_open_investigation_marks_manual`.
* `tests/test_aiops_metrics.py` — the investigation-derived metrics.
