# Design: the AI-Ops Maturity Model in Daalu

Status: implemented on branch `feat/ai-ops-maturity` (2026-11). This is the
contract every piece of the feature was built against. The narrative
versions live in the engineer book, Part XV (chapters 81–90), and the
user guide, Part IX (chapters 51–58).

## 1. The standard: AI-Ops Maturity Model (AOMM) v0.1

DevOps spread because it came with shared metrics (DORA), a picture of what
good looks like, and practices expressed as code. AOMM is the same thing for
how infrastructure teams work with AI, and it leads with the numbers.

**Metrics first.** Five TRACE metrics (time to **T**riage, time to **R**esolve, **A**greement rate, **C**hange-failure rate, **E**scalation rate) say how well a team's AI operations
perform; the rest are supporting metrics that explain them (section 7, spec
§3):

| Group | TRACE metric (API field) | DORA counterpart |
|---|---|---|
| Speed | Time to **T**riage (`median_time_to_triage_s`) | Lead time for changes |
| Speed | Time to **R**esolve (`median_time_to_resolve_s`) | Time to restore service |
| Quality | **A**greement rate (`agreement_rate`) | — (is the AI right?) |
| Quality | **C**hange-failure rate (`change_failure_rate`) | Change-failure rate |
| Autonomy | **E**scalation rate (`escalation_rate`) | — (new to AI operations) |

Supporting: `alerts_total`, `ai_triage_rate` (coverage), `verdict_coverage`
(feedback), `proposal_approval_rate` (trust), `runbooks_published`,
`runbooks_generated`, `eval_runs`, `eval_mean_score` (learning),
`audit_chain_ok` (integrity).

**Then levels, as thresholds on those metrics.**

| Level | Name | Definition | What Daalu provides |
|---|---|---|---|
| 0 | Ad hoc | Individuals paste logs into a chatbot. No shared context, no record. | — |
| 1 | Assisted | AI reads alerts, logs, metrics and explains them. Read-only. | Auto-investigation, read-only tools, briefings |
| 2 | Proposing | AI names the root cause and proposes a fix; every change is human-approved. | Pending actions, approve/reject, verification |
| 3 | Trusted | Policy lets low-risk, proven actions run automatically. | Autonomy policy as code, trust thresholds, previews, multi-approver |
| 4 | Learning | Every incident improves the next: verdicts, runbooks, evals, metrics. | Investigations + verdicts, runbooks as code, eval harness, metrics |
| 5 | Autonomous | AI resolves most routine incidents; humans own policy and exceptions. | Auto-execution with guardrails (kill switch, circuit breaker, freeze windows, rate limit), audit chain, exception queue, MCP for any agent |

The level a team is *at* is the highest level whose criteria are all met
(section 7). Levels are cumulative. Three TRACE metrics gate a level
(change-failure rate `change_failure_rate` → L3; escalation rate
`escalation_rate` and agreement rate `agreement_rate` → L5); the two speed metrics gate none, on purpose, and are tracked against
the team's own history.

## 2. Data model (`src/daalu_automation/models/aiops.py`, migration `0034_aiops_maturity`)

| Table | Purpose |
|---|---|
| `autonomy_policies` | One row per saved version of a tenant's policy YAML. Newest `version` is active. Columns: `version`, `yaml`, `updated_by`, `note`. |
| `aiops_tenant_state` | One row per tenant: `kill_switch` (bool), `kill_switch_by`, `kill_switch_at`, `breaker` (JSON: rule_id → tripped-until ISO time). |
| `investigations` | One AI investigation per alert (`alert_id` unique). Timestamps, root cause, counters, resolution, verdict. |
| `audit_events` | Append-only, hash-chained per tenant. |
| `runbooks` | Runbook-as-code: markdown body + `match` (JSON) + `status` draft/published + `source` manual/generated/imported. |
| `eval_runs` | One row per eval scenario run, with score and details. |
| `maturity_assessments` | Self-assessment answers (JSON) + computed level. |

`alert_actions` gains: `policy_mode`, `policy_rule`, `policy_reason`,
`approvals_required` (int, default 1), `approvals` (JSON list of
`{user_id, email, at}`), `preview` (JSON), `auto_executed` (bool),
`verified_resolved` (bool, nullable), `actor` (str: `agent`, `mcp:<email>`).

### Investigation columns
`alert_id`, `started_at`, `triaged_at` (first answer with a
`## Root cause` section), `root_cause` (text of that section),
`actions_proposed`, `actions_approved`, `actions_rejected`, `actions_auto`,
`resolution` (`open` | `auto` | `approved` | `manual` | `unresolved`),
`resolved_at`, `verified` (bool, nullable), `runbook_ids` (JSON list),
`verdict` (`correct` | `partial` | `wrong` | null), `verdict_by`,
`verdict_at`, `verdict_note`, `model` (str).

### Audit event columns
`seq` (per-tenant monotonic), `kind`, `actor`, `subject` (e.g. alert id),
`data` (JSON), `prev_hash`, `hash`. `hash = sha256(prev_hash + canonical_json({seq, kind, actor, subject, data, created_at}))`.
Writes take `pg_advisory_xact_lock(hashtext(tenant_id))` so the chain never forks.
Kinds: `tool.read`, `action.proposed`, `action.denied`, `action.approved`,
`action.approval_recorded`, `action.rejected`, `action.executed`,
`action.auto_executed`, `action.verified`, `policy.updated`,
`policy.kill_switch`, `policy.breaker_tripped`, `investigation.verdict`,
`runbook.published`, `runbook.drafted`, `mcp.call`, `eval.recorded`.

## 3. Autonomy policy as code (`core/aiops/policy.py`)

```yaml
version: 1
investigate_automatically: true     # agent host starts triage on every new alert
defaults:
  mode: approve                     # deny | approve | auto | auto_after_trust
  approvers: 1
guardrails:
  max_auto_actions_per_hour: 10
  circuit_breaker:                  # an auto action that fails verification
    cooldown_minutes: 60            # demotes its rule to `approve` for this long
  freeze_windows:                   # auto → approve inside these windows
    - days: [sat, sun]
      start: "00:00"
      end: "23:59"
      tz: UTC
rules:
  - id: dev-restarts
    match: {tools: [restart_deployment, delete_pod], clusters: ["dev-*"], namespaces: ["*"]}
    mode: auto
  - id: prod-rollback
    match: {tools: [rollout_undo], namespaces: ["prod-*", "default"]}
    mode: approve
    approvers: 2
  - id: scale-earned
    match: {tools: [scale_deployment]}
    mode: auto_after_trust
    trust: {min_successes: 10, max_failure_rate: 0.05, window_days: 30}
  - id: no-patches-in-kube-system
    match: {tools: [patch_resource, delete_pod], namespaces: [kube-system]}
    mode: deny
```

* Applies to write tools only (anything `kube_tools.tool_requires_approval`
  says is a write). Read tools always run.
* First matching rule wins; no match → `defaults`. Globs are `fnmatch`.
  An omitted match key matches everything.
* No saved policy → built-in default: everything `approve`, 1 approver,
  `investigate_automatically: false`. This is exactly the pre-AOMM
  behaviour.
* `decide(...)` returns `Decision(mode, rule_id, reason, approvers)` where
  `mode` is the *effective* mode after guardrails: `deny`, `approve` or
  `auto`. Order: kill switch → rule's mode → `auto_after_trust` resolved
  via trust stats → breaker → freeze window → hourly rate limit.
* Trust stats per rule: actions matched by that rule in `window_days` that
  executed, with `verified_resolved` true (success) / false (failure).
* `deny` → the write is answered with a tool error explaining the policy;
  no `AlertAction` is created (an `action.denied` audit event is).
* `auto` → `AlertAction` is created `approved`, `auto_executed=True`,
  and run through the same `_run_approved_action` path (same verification).
  If verification fails, the rule's breaker trips.
* Multi-approver: approve endpoint records an approval; the action runs
  only when `len(approvals) >= approvals_required`, from distinct users.

## 4. Investigations and the learning loop (`core/aiops/investigations.py`)

* `_record_assistant_turn` calls `ensure_investigation(alert)` and updates
  counters as actions are proposed / approved / rejected / auto-run.
* The first assistant message with a `## Root cause` heading sets
  `triaged_at` and `root_cause`.
* `_run_approved_action` writes `verified_resolved` on the action and, if
  resolved, `resolution` (`auto` if the action was auto-executed, else
  `approved`) + `resolved_at` on the investigation. Alert resolved with no
  executed action → `manual`.
* Verdicts: `POST /aiops/investigations/{id}/verdict`.
* On a verified resolution with no published runbook matching, a draft
  runbook is generated (section 5).

## 5. Runbooks as code (`core/aiops/runbooks.py`)

File format (also what `daalu runbooks import <dir>` reads):

```markdown
---
id: dummy-app-down
title: DummyApp is down
match:
  alertname: [DummyAppDown]
  labels: {namespace: daalu-demo}
allowed_actions: [rollout_undo, restart_deployment]
---
## Symptoms
...
## Diagnosis
...
## Fix
...
```

* Matching: alert `metadata.labels.alertname` (or alert title) against
  `match.alertname`; every key in `match.labels` must equal the alert label.
* Published matching runbooks are injected into the triage context and
  their ids recorded on the investigation.
* Generated drafts are `status: draft` until a human publishes them.

## 6. Change context and previews

* `recent_changes` read tool (`core/aiops/change_context.py`): Deployment
  rollout revisions (ReplicaSet creation times, image changes),
  `ScalingReplicaSet` events and Argo CD `Application` history in the
  namespace (or cluster) for the last N hours.
* `preview_write(tool, input)` (`core/aiops/preview.py`): best-effort,
  never raises. `scale_deployment` → current → target replicas;
  `restart_deployment` → pods to be recycled; `rollout_undo` → current vs
  target revision images; `delete_pod` → pod, owner, whether it'll be
  recreated; `patch_resource` → server-side dry-run result and changed
  keys. Output: `{summary, blast_radius: {pods, deployments}, details}`.
  Stored on `AlertAction.preview`, shown on the approve card.

## 7. Metrics and maturity (`core/aiops/metrics.py`, `core/aiops/maturity.py`)

`GET /aiops/metrics?days=30` returns the metrics below. They come first;
the level is derived from them.

**TRACE metrics** (spec §3.1), reported together for the same window:

| TRACE metric | API field | Group | Definition |
|---|---|---|---|
| Time to **T**riage | `median_time_to_triage_s` | Speed | `triaged_at − alert.created_at`, median |
| Time to **R**esolve | `median_time_to_resolve_s` | Speed | `resolved_at − alert.created_at`, median |
| **A**greement rate | `agreement_rate` | Quality | (`correct` + 0.5·`partial`) / verdicts |
| **C**hange-failure rate | `change_failure_rate` | Quality | Executed actions with `verified_resolved=false` / executed actions |
| **E**scalation rate | `escalation_rate` | Autonomy | Investigations resolved `approved` or `manual` / resolved investigations |

**Supporting metrics** (spec §3.2):

| Metric | Group | Definition |
|---|---|---|
| `alerts_total` | Coverage | Alerts created in the window |
| `ai_triage_rate` | Coverage | Alerts with an investigation / alerts |
| `verdict_coverage` | Feedback | Investigations with a verdict / investigations |
| `proposal_approval_rate` | Trust | Approved / (approved + rejected) human-gated actions |
| `runbooks_published`, `runbooks_generated` | Learning | Counts |
| `eval_runs`, `eval_mean_score` | Learning | In the window |
| `audit_chain_ok` | Integrity | Chain verifies |

**Mapping to levels.** Automatic level = all criteria of a level and every
level below:

* **L1**: `ai_triage_rate ≥ 0.5` and `alerts_total ≥ 5`.
* **L2**: ≥ 1 human-approved action executed and `proposal_approval_rate` known.
* **L3**: `change_failure_rate ≤ 0.15`, active policy with at least one `auto`/`auto_after_trust` rule, ≥ 1 auto action executed.
* **L4**: `verdict_coverage ≥ 0.3`, `runbooks_published ≥ 1`, `eval_runs ≥ 1` in the window.
* **L5**: `escalation_rate ≤ 0.5`, `agreement_rate ≥ 0.8`, `audit_chain_ok`, guardrails configured (circuit breaker + rate limit).

Time to triage (`median_time_to_triage_s`) and time to resolve
(`median_time_to_resolve_s`) gate no level:
they depend on each team's systems and alert mix, so no single threshold is
fair. They are the outcome the levels are meant to improve.

`GET /aiops/maturity` returns `{level, name, criteria: [{level, id, label, met, value}], next_steps: [...], assessment}`.
The self-assessment (`POST /aiops/maturity/assessment`) covers the
organisational practices the data can't see (on-call trusts AI output,
post-incident reviews include the AI, policy reviewed quarterly, …) and is
reported side by side; the headline level is the automatic one.

## 8. Eval harness (`src/daalu_automation/evals/`, scenarios in `evals/scenarios/`)

```yaml
id: crashloop-bad-image
title: Deployment rolled to an image that crashes
cluster: kind-daalu-demo
namespace: daalu-demo
setup: []                       # kubectl args run before inject (idempotent)
inject:
  - [set, image, deploy/dummy-app, dummy=busybox:1.36, --namespace, daalu-demo]
alert: DummyAppDown             # wait for this alertname in Daalu
timeout_s: 600
expect:
  root_cause_any: [image, crash, CrashLoopBackOff, revision]
  fix_tools_any: [rollout_undo]
  forbidden_tools: [delete_namespace]
cleanup:
  - [rollout, undo, deploy/dummy-app, --namespace, daalu-demo]
```

`daalu eval list` / `daalu eval run <id> --label <model-name>` drives the
scenario against a live Daalu (`DAALU_URL`, `DAALU_TOKEN`) and a cluster
(`--kubeconfig`), triggers a forced triage, grades the transcript
(root cause keywords 40 %, a correct fix tool proposed 40 %, no forbidden
tool 10 %, diagnosed within timeout 10 %) and records the run via
`POST /aiops/evals/runs`. `daalu eval grade` re-grades a saved transcript
offline (used by the unit tests).

## 9. MCP server (`core/aiops/mcp.py`, `POST {prefix}/mcp`, `daalu mcp`)

Model Context Protocol (protocol version `2025-06-18`), JSON-RPC 2.0.
Methods: `initialize`, `notifications/initialized`, `ping`, `tools/list`,
`tools/call`. HTTP transport: `POST {prefix}/mcp`, authenticated with a
personal access token (`Authorization: Bearer dpat_…`), JSON responses.
Stdio transport: `daalu mcp --url … --token …` bridges stdin/stdout to the
HTTP endpoint, so Claude Code / Cursor / any MCP client can use it.

Tools: `list_alerts`, `get_investigation`, `list_clusters`,
`run_read_tool` (read-only kube/prom/loki tools; writes refused),
`recent_changes`, `propose_action` (attaches a write to an alert as a
pending action, through the same policy — an external agent can never
bypass approval; `auto` is downgraded to `approve` for MCP callers unless
the rule sets `allow_external_auto: true`), `get_policy`,
`simulate_policy`, `list_runbooks`, `get_runbook`, `get_maturity`,
`submit_verdict`. Every call writes an `mcp.call` audit event.

## 10. Open specs (`spec/`)

`spec/README.md` (AOMM v0.1 text: §3 metrics, §4 levels, §5 practices,
§6 artefacts), JSON Schemas:
`autonomy-policy.schema.json`, `action-proposal.schema.json`,
`audit-event.schema.json`, `investigation.schema.json`,
`runbook.schema.json` (frontmatter), `eval-scenario.schema.json`.
The implementation validates policy and scenarios against these schemas.

## 11. API surface (all under `{prefix}` = `/api/v1`)

| Method | Path | Owner file |
|---|---|---|
| GET/PUT | `/aiops/policy` | `routers/aiops_policy.py` |
| GET | `/aiops/policy/history` | 〃 |
| POST | `/aiops/policy/simulate` | 〃 |
| POST | `/aiops/policy/kill-switch` | 〃 |
| GET | `/aiops/trust` | 〃 |
| GET | `/aiops/investigations`, `/aiops/investigations/{id}` | `routers/aiops_investigations.py` |
| POST | `/aiops/investigations/{id}/verdict` | 〃 |
| GET | `/aiops/audit`, `/aiops/audit/verify` | `routers/aiops_audit.py` |
| GET/POST | `/aiops/runbooks`; GET/PUT/DELETE `/aiops/runbooks/{id}`; POST `/aiops/runbooks/{id}/publish` | `routers/aiops_runbooks.py` |
| GET | `/aiops/metrics`, `/aiops/maturity`; POST `/aiops/maturity/assessment` | `routers/aiops_metrics.py` |
| GET | `/aiops/evals/scenarios`, `/aiops/evals/runs`; POST `/aiops/evals/runs` | `routers/aiops_evals.py` |
| POST | `/mcp` | `routers/mcp.py` |

Writes to policy, kill switch and runbook publish require `user.is_admin`.

## 12. UI

Sidebar, grouped by task: **Respond** → Investigations (`/investigations`);
**Automate** → Runbooks (`/runbooks`), Autonomy policy (`/autonomy`);
**Insights** → Maturity (`/maturity`), Evals (`/evals`), Audit log (`/audit`).
The alert chat approve card shows
the policy decision, approvals `n/N`, and the preview; the alert page shows
the investigation verdict buttons.

## 13. Response shapes for the endpoints built in parallel

Rates are floats in 0..1, or `null` when the denominator is 0.

* `RunbookOut`: `{id, slug, title, body, match: {alertname: [], labels: {}}, allowed_actions: [], status, source, version, from_investigation_id, published_by, published_at, created_at, updated_at}`.
  `GET /aiops/runbooks?status=` → `RunbookOut[]`; `POST` / `PUT /{id}` accept either
  `{title, body, match, allowed_actions, slug?, status?}` or `{markdown}` (a whole
  runbook file with frontmatter); `POST /{id}/publish`; `DELETE /{id}`;
  `GET /aiops/runbooks/match?alert_id=` → `RunbookOut[]`.
* `GET /aiops/metrics?days=30` → `{days, alerts_total, investigations_total, ai_triage_rate, median_time_to_triage_s, agreement_rate, verdict_coverage, verdicts: {correct, partial, wrong}, proposal_approval_rate, actions: {proposed, approved, rejected, auto, executed, failed_verification}, change_failure_rate, escalation_rate, resolutions: {auto, approved, manual, unresolved, open}, median_time_to_resolve_s, runbooks_published, runbooks_generated, eval_runs, eval_mean_score, audit_chain_ok, daily: [{date, alerts, investigations, auto_resolved, approved_resolved}]}`.
* `GET /aiops/maturity?days=30` → `{level, name, levels: [{level, name, description, met}], criteria: [{level, id, label, met, value, target}], next_steps: [str], metrics: {…as above}, assessment: {level, answers, created_at, created_by} | null, questions: [{id, level, text}]}`.
  `POST /aiops/maturity/assessment` `{answers: {question_id: bool}}` → the assessment.
* `GET /aiops/evals/scenarios` → `[{id, title, description, namespace, alert, expect}]`;
  `GET /aiops/evals/runs?scenario_id=&label=` → `[{id, scenario_id, label, alert_id, started_at, finished_at, score, passed, details}]`;
  `GET /aiops/evals/leaderboard` → `[{label, runs, mean_score, pass_rate, scenarios}]`;
  `POST /aiops/evals/runs` with the run fields → the run.

Internal functions other modules call:
`runbooks.list_runbooks(db, tenant_id, status=None)`, `runbooks.get_runbook(db, tenant_id, id_or_slug)`,
`runbooks.match_for_alert(db, tenant_id, alert)`, `runbooks.maybe_draft_from_investigation(db, *, tenant_id, alert, investigation)`,
`metrics.compute_metrics(db, tenant_id, days=30) -> dict`, `maturity.compute(db, tenant_id, days=30) -> dict`,
`change_context.recent_changes(*, tenant_id, cluster, namespace, hours=24) -> str`,
`preview.preview_write(tool, tool_input, *, tenant_id, cluster) -> dict`.

## 14. Documentation map

Engineer book, Part XV `docs/book-engineer/15-ai-ops-maturity/`:

| Ch. | File | Topic |
|---|---|---|
| 81 | `81-the-maturity-model.md` | AOMM: levels, criteria, how Daalu maps to them |
| 82 | `82-autonomy-policy.md` | Policy as code: schema, decide(), storage, API |
| 83 | `83-guardrails-and-auto-execution.md` | auto / auto_after_trust, trust stats, kill switch, breaker, freeze, rate cap, multi-approver |
| 84 | `84-investigations-and-verdicts.md` | Investigation record, outcome verification, verdicts, auto-investigation |
| 85 | `85-runbooks-as-code.md` | Format, matching, injection, drafting, import CLI |
| 86 | `86-change-context-and-previews.md` | `recent_changes` tool, previews / blast radius |
| 87 | `87-audit-log.md` | Hash chain, kinds, verification |
| 88 | `88-metrics-and-maturity-scoring.md` | Metric definitions, automatic level, self-assessment |
| 89 | `89-eval-harness.md` | Scenarios, runner, grading, leaderboard |
| 90 | `90-mcp-server-and-open-specs.md` | MCP transports and tools, `spec/` schemas |

User guide, Part IX `docs/user-guide/09-ai-operations-maturity/`:

| Ch. | File | Topic |
|---|---|---|
| 51 | `51-where-is-your-team.md` | The maturity model for your team, reading the Maturity page |
| 52 | `52-setting-your-autonomy-policy.md` | Writing and testing a policy, kill switch |
| 53 | `53-investigations-and-verdicts.md` | Reviewing investigations, giving verdicts |
| 54 | `54-runbooks.md` | Writing, importing, publishing drafts |
| 55 | `55-metrics-dashboard.md` | What each metric means |
| 56 | `56-audit-log.md` | Reading and verifying the audit log |
| 57 | `57-running-evals.md` | Benchmarking your AI SRE |
| 58 | `58-connect-your-agent-with-mcp.md` | Claude Code / Cursor through Daalu |

Standard: `spec/README.md` (AOMM v0.1) and `docs/maturity-model.md` (team-facing summary).
