# 88. Metrics and maturity scoring

DORA gave DevOps four numbers everyone could compute. AOMM's equivalent is
the metrics in `core/aiops/metrics.py`: how much of the alert load the AI
triages, how fast and how correctly it diagnoses, how often its changes are
approved and how often they fail, how much resolves without a human. The
maturity level (chapter 81) is a set of thresholds on these numbers.

Code: `core/aiops/metrics.py`, `core/aiops/maturity.py`,
`api/routers/aiops_metrics.py`.

---

## The window

`collect(db, tenant_id, days)` (`core/aiops/metrics.py:64`), `days` clamped
to 1–365:

* **alerts**: created in the last `days` days;
* **investigations**: those of the alerts in the window (joined on
  `alert.created_at`, not `started_at`);
* **write actions**: `requires_approval=True` actions *created* in the
  window;
* **eval runs**: *started* in the window;
* **runbook counts**: current totals, not windowed;
* **audit chain**: verified in full on every call.

Rates are floats in 0..1 rounded to four places, or `null` when the
denominator is 0 (`_rate`, `:33`). Medians are rounded to 0.1 s. `null` is
deliberate: "0 % approval rate" and "no decisions yet" are different facts,
and the UI shows `—` for the second.

---

## Definitions, as computed

| Metric | Computation |
|---|---|
| `alerts_total` | alerts in the window |
| `investigations_total` | investigations of those alerts |
| `ai_triage_rate` | alerts with an investigation / `alerts_total` |
| `median_time_to_diagnosis_s` | median of `diagnosed_at − alert.created_at` over diagnosed investigations |
| `verdicts` | counts of `correct`, `partial`, `wrong` |
| `rca_agreement_rate` | `(correct + 0.5 · partial) / verdicts` |
| `verdict_coverage` | verdicts / `investigations_total` |
| `actions.proposed` | write actions created in the window |
| `actions.approved` | human-gated (not `auto_executed`) actions in `approved`, `executed` or `failed` |
| `actions.rejected` | human-gated actions in `rejected` |
| `actions.auto` | `auto_executed` actions |
| `actions.executed` | actions in `executed` or `failed` (they ran) |
| `actions.failed_verification` | executed actions with `verified_resolved = false` |
| `proposal_approval_rate` | approved / (approved + rejected) |
| `ai_change_failure_rate` | `failed_verification` / `executed` |
| `resolutions` | counts by effective resolution (`effective_resolution`, `:47`: an `open` investigation whose alert is resolved counts as `manual` at the alert's `resolved_at`) |
| `auto_resolution_rate` | `auto` / (`auto` + `approved` + `manual`) |
| `median_time_to_resolve_s` | median of `resolved_at − alert.created_at` over resolved investigations |
| `runbooks_published`, `runbooks_generated` | current totals by status / by `source=generated` |
| `eval_runs`, `eval_mean_score` | count and mean score of runs started in the window |
| `audit_chain_ok` | `verify_chain(...).ok` |
| `daily` | per UTC day, oldest first: `alerts`, `investigations` (by `started_at`), `auto_resolved`, `approved_resolved` (by resolution day) |

Choices worth defending:

* **Time to diagnosis is measured from the alert, not from the
  investigation start.** An AI that diagnoses in 30 s but only starts when
  someone opens the alert two hours later has a two-hour time to diagnosis
  in any sense that matters to on-call. This is what makes
  `investigate_automatically` show up in the number.
* **Change failure rate counts verification, not tool errors.** A write that
  ran cleanly and didn't fix anything is the failure that matters (the
  DORA "change failure" sense); a tool error also fails verification, so it
  is counted too.
* **Auto-resolution rate's denominator includes manual resolutions.** The
  question is "of everything that got resolved, how much did the AI fix
  without a human", so alerts that cleared on their own or were fixed by
  hand count against it.
* **The approval rate excludes automatic actions.** They were never put to
  a human; counting them as approvals would inflate the trust signal with
  the policy's own decisions.

Known inaccuracies:

* Eval runs marked invalid (answered by the wrong model, chapter 89) are
  excluded from `eval_runs` and `eval_mean_score`, as on the leaderboard
  (`test_invalid_eval_runs_do_not_count`).
* An action whose execution crashed is recorded with
  `verified_resolved = false` (chapter 83, "Hardening"), so it counts in
  `failed_verification` and the change-failure rate.

---

## The automatic level

`maturity.compute` (`core/aiops/maturity.py:254`) returns:

```
{level, name,
 levels:    [{level, name, description, met, achieved}],
 criteria:  [{level, id, label, met, value, target}],
 next_steps: [str],
 metrics:   {...as above},
 assessment: {id, level, name, answers, created_at, created_by} | null,
 questions: [{id, level, text}]}
```

`levels[].met` is "this level's own criteria hold"; `achieved` is the
cumulative "this level is reached". The UI ladder shows both, so a team
can see that it already meets the Level 4 criteria while being held at
Level 2 by Level 3.

The fourteen criteria and their thresholds are listed in chapter 81.
`build_criteria` (`core/aiops/maturity.py:103`) is pure: it takes the
metrics, two counts that aren't in the public metrics shape
(`human_executed`, `auto_executed`, from `collect`'s `extras`), and the
saved-policy facts (`_policy_facts`, `:182`). Each criterion carries a
`step` sentence used by `next_steps` (`:243`): unmet criteria of the next
level, then of the one after, at most six.

The design doc's response shape omits `achieved`, the assessment's `id` and
`name`, and the criterion's `target`; the code returns them and the UI uses
them.

### Why these thresholds

They are v0.1 judgement calls, chosen so each level is reachable by a real
team in weeks, not a benchmark: 50 % triage (half your alerts reach the AI),
≤ 15 % change failure (the upper bound of DORA's "high performer" band),
30 % verdict coverage (a sample big enough to mean something without
grading every alert), 80 % RCA agreement and 50 % auto-resolution for
Level 5. They live in one function so that a v0.2 can change them in one
place; the spec's versioning section (§7) lets pre-1.0 minor versions
tighten them and says implementations should report the thresholds they
applied, which `criteria[].target` does.

---

## Self-assessment

`POST /aiops/maturity/assessment {answers: {question_id: bool}}`
(`api/routers/aiops_metrics.py:41`) → `save_assessment`
(`core/aiops/maturity.py:225`): unknown ids are a 422, unanswered questions
are stored as `false`, the level is computed with the same cumulative rule,
and a new `maturity_assessments` row is inserted (history is kept; the
latest is reported). Any user may submit one; it writes no audit event.

The 16 questions (three per level for L1–L3 and L5, four for L4) are in
`QUESTIONS` (`core/aiops/maturity.py:42`). They ask about the organisation:
whether on-call reviews the AI's root cause before acting, whether the
policy goes through code review, whether post-incident reviews include the
AI's investigation, whether there is an error budget for automatic changes.

---

## API

| Method | Path | Line |
|---|---|---|
| GET | `/aiops/metrics?days=30` | `api/routers/aiops_metrics.py:23` |
| GET | `/aiops/maturity?days=30` | `:32` |
| POST | `/aiops/maturity/assessment` | `:41` |

`days` is 1–365. The MCP `get_maturity` tool returns the same payload.

The UI is `frontend/app/maturity/page.tsx` with
`frontend/components/aiops/maturity-parts.tsx` (ladder, daily trend chart,
assessment form) and a 7/30/90-day window switch.

---

## Testing

`tests/test_aiops_metrics.py`: `test_metrics_and_levels` seeds alerts,
investigations, actions, runbooks and eval runs and checks every metric and
the computed level; `test_empty_tenant_is_level_0`;
`test_level_rules_are_cumulative` (pure); `test_metrics_maturity_api`
through HTTP.
