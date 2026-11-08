# AI-Ops Maturity Model (AOMM) — v0.1

Status: draft standard, version 0.1 (2026-11).
Reference implementation: [Daalu](https://github.com/kiwueke1/daalu).

## 1. Purpose

DevOps spread because it came with three things: shared metrics (DORA), a
picture of what good looks like, and practices expressed as code. Teams
adopting AI for infrastructure operations have none of these. Each vendor
defines its own "autonomy", nobody can say whether one team's AI is doing
better than another's, and changes made by AI agents are hard to review after
the fact.

AOMM is a vendor-neutral model of how infrastructure teams work with AI. It
starts from numbers, the way DORA does:

* **Metrics** (section 3): five TRACE metrics that say how well a team's AI
  operations perform, plus supporting metrics that explain them. Every one
  has an exact definition, so any implementation computes the same value.
* **Levels** (section 4): where a team is and what the next step is. Each
  level is a set of thresholds on the metrics, so levels are measured, not
  claimed.
* **Practices** (section 5) that a team adopts to move the metrics, each
  expressed as a machine-readable artefact.
* **Artefact schemas** (section 6), so policies, runbooks, investigations,
  audit logs and evals move between tools.

Any platform, internal tool or agent framework can implement AOMM. Nothing in
this document requires a particular vendor, model or orchestrator.

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## 2. Terms

* **Alert** — a signal that something is wrong (from Prometheus, a cloud
  monitor, a ticket, …). AOMM treats it as the unit of incident work.
* **Agent** — an AI system that investigates alerts and proposes or takes
  actions. It may be built into a platform or connect from outside.
* **Read action** — an action that cannot change system state (logs, events,
  metrics, descriptions, history).
* **Write action** — any action that can change system state (rollback,
  scale, restart, delete, patch, a non-GET HTTP call).
* **Investigation** — one agent's analysis of one alert, from start to
  resolution (section 6.4).
* **Action proposal** — a write action an agent wants to take, together with
  the policy decision and its outcome (section 6.2).
* **Autonomy policy** — the code that decides, for every write action,
  whether it is denied, needs human approval or may run automatically
  (section 6.1).
* **Verification** — a read-only check, after a write executes, of whether
  the affected system recovered.
* **Implementation** — software that conforms to this standard.

## 3. Metrics

All metrics are computed over a window of `days` days (default 30) ending now.
Rates are numbers in `[0, 1]`, or `null` when the denominator is 0. Medians of
empty sets are `null`.

### 3.1 The five TRACE metrics

DORA measures software delivery with two speed metrics (deployment frequency,
lead time) and two stability metrics (change-failure rate, time to restore).
AOMM measures AI operations the same way, and adds the one thing DORA has no
need for: how much of the work still needs a human. **TRACE** is their initials: **T**ime to triage, time to **R**esolve, **A**greement rate, **C**hange-failure rate, **E**scalation rate.

| Group | TRACE metric (API field) | Question it answers | Better is | DORA counterpart |
|---|---|---|---|---|
| Speed | **T**ime to triage (`median_time_to_diagnosis_s`) | How long from an alert firing to the AI naming a root cause? | Lower | Lead time for changes |
| Speed | Time to **R**esolve (`median_time_to_resolve_s`) | How long from an alert firing to the incident being resolved? | Lower | Time to restore service |
| Quality | **A**greement rate (`rca_agreement_rate`) | How often do humans agree with the AI's root cause? | Higher | — (is the AI right?) |
| Quality | **C**hange-failure rate (`ai_change_failure_rate`) | Of the changes the AI made, how many failed verification? | Lower | Change-failure rate |
| Autonomy | **E**scalation rate (`escalation_rate`) | Of resolved incidents, how many still needed a human? | Lower | — (new to AI operations) |

Read the TRACE metrics together. Speed with poor quality is a fast wrong answer.
Autonomy with a high change-failure rate is unattended damage. A team is doing
well when time to triage and time to resolve fall while agreement rate
stays high and the change-failure rate stays low, and
escalation rate falls on top of that.

Implementations claiming AOMM conformance MUST report the five TRACE metrics
together, for the same window.

### 3.2 Supporting metrics

Supporting metrics explain the TRACE metrics and show whether the practices
behind them are in place. Some are inputs to the TRACE metrics (no verdicts
means no agreement rate, `rca_agreement_rate`); others count adoption of a practice.

| Group | Metric | Why it matters |
|---|---|---|
| Coverage | `alerts_total`, `ai_triage_rate` | How much of the incident work the AI sees and investigates. |
| Feedback | `verdict_coverage` | How much of the AI's work humans grade; the sample behind `rca_agreement_rate`. |
| Trust | `proposal_approval_rate` | How often humans accept the AI's proposed changes. |
| Learning | `runbooks_published`, `runbooks_generated`, `eval_runs`, `eval_mean_score` | Whether each incident improves the next. |
| Integrity | `audit_chain_ok` | Whether the record of what the AI did can be trusted. |

### 3.3 Definitions

| Metric | Kind | Definition |
|---|---|---|
| `median_time_to_diagnosis_s` | Key | Median of `diagnosed_at − alert.created_at` (seconds) over investigations with a diagnosis. |
| `median_time_to_resolve_s` | Key | Median of `resolved_at − alert.created_at` (seconds) over resolved investigations. |
| `rca_agreement_rate` | Key | (`correct` + 0.5 × `partial`) ÷ investigations with a verdict. |
| `ai_change_failure_rate` | Key | Executed write actions with `verified_resolved = false` ÷ executed write actions. |
| `escalation_rate` | Key | Investigations resolved `approved` or `manual` ÷ resolved investigations (`auto`, `approved`, `manual`): the share that needed a human. |
| `auto_resolution_rate` | Supporting | Investigations resolved `auto` ÷ resolved investigations; `1 − escalation_rate`, kept for compatibility. |
| `alerts_total` | Supporting | Alerts created in the window. |
| `ai_triage_rate` | Supporting | Alerts in the window that have an investigation ÷ `alerts_total`. |
| `verdict_coverage` | Supporting | Investigations with a verdict ÷ investigations. |
| `proposal_approval_rate` | Supporting | Human-approved ÷ (human-approved + rejected) action proposals. Automatic actions are excluded. |
| `runbooks_published` | Supporting | Runbooks in status `published`. |
| `runbooks_generated` | Supporting | Runbooks drafted from an investigation. |
| `eval_runs` | Supporting | Eval runs in the window. |
| `eval_mean_score` | Supporting | Mean score (0..1) of those runs. |
| `audit_chain_ok` | Supporting | The audit chain verifies end to end. |

Investigation `resolution` values: `open` (in progress), `auto` (a verified
automatic action fixed it), `approved` (a verified human-approved action fixed
it), `manual` (the alert resolved with no executed action), `unresolved`
(actions ran but verification failed).

## 4. Levels

| Level | Name | Definition |
|---|---|---|
| 0 | Ad hoc | Individuals paste logs into a chatbot. No shared context, no record. |
| 1 | Assisted | AI reads alerts, logs and metrics and explains them. Read-only. |
| 2 | Proposing | AI names the root cause and proposes a fix; every change is human-approved and verified. |
| 3 | Trusted | Policy lets low-risk, proven actions run automatically. |
| 4 | Learning | Every incident improves the next: verdicts, runbooks, evals, metrics. |
| 5 | Autonomous | AI resolves most routine incidents; humans own policy and exceptions. |

Levels are cumulative. A team is **at** the highest level whose entry
criteria, and the criteria of every level below it, are all met over the
measurement window (default: the last 30 days). Criteria are thresholds on the
metrics in section 3; they are measured, not self-declared.

### 4.1 How the metrics map to the levels

Each row below is the measurable test for the level of the same number in the
table above. The definition says what the level means; the criteria are the
signals in the metrics that show a team is there. Where a definition asks for
something the data cannot show (for example, "humans own policy and
exceptions" at level 5), the self-assessment in section 4.3 covers it.

| Level | Criteria (all must hold) |
|---|---|
| L0 · Ad hoc | None. Level 0 is the starting point, not something a team reaches. |
| L1 · Assisted | `alerts_total ≥ 5` and `ai_triage_rate ≥ 0.5`. |
| L2 · Proposing | At least one human-approved write action executed, and `proposal_approval_rate` is defined (at least one human decision recorded). |
| L3 · Trusted | An active autonomy policy with at least one rule (or the default mode) set to `auto` or `auto_after_trust`; at least one automatic action executed; `ai_change_failure_rate ≤ 0.15`. |
| L4 · Learning | `verdict_coverage ≥ 0.3`; `runbooks_published ≥ 1`; `eval_runs ≥ 1`. |
| L5 · Autonomous | `escalation_rate ≤ 0.5`; `rca_agreement_rate ≥ 0.8`; `audit_chain_ok` is true; guardrails configured (a circuit breaker and an hourly rate limit on automatic actions). |

The same mapping, read from the metric side:

| Metric | Kind | Gates | Threshold |
|---|---|---|---|
| `median_time_to_diagnosis_s` | Key | — | Tracked at every level; no threshold |
| `median_time_to_resolve_s` | Key | — | Tracked at every level; no threshold |
| `ai_change_failure_rate` | Key | L3 | ≤ 0.15 |
| `escalation_rate` | Key | L5 | ≤ 0.5 |
| `rca_agreement_rate` | Key | L5 | ≥ 0.8 |
| `alerts_total` | Supporting | L1 | ≥ 5 |
| `ai_triage_rate` | Supporting | L1 | ≥ 0.5 |
| `proposal_approval_rate` | Supporting | L2 | Defined |
| `verdict_coverage` | Supporting | L4 | ≥ 0.3 |
| `runbooks_published` | Supporting | L4 | ≥ 1 |
| `eval_runs` | Supporting | L4 | ≥ 1 |
| `audit_chain_ok` | Supporting | L5 | True |

Three criteria are facts about executed actions and the saved policy rather
than metrics: at least one human-approved action executed (L2), at least one
automatic action executed and an `auto` rule in the policy (L3), and
guardrails configured (L5).

The two speed metrics gate no level, on purpose. How fast a team diagnoses and
resolves depends on its systems and its alert mix, so no single threshold fits
every team. They are the result climbing the levels is meant to improve, and
they SHOULD be tracked against the team's own history.

### 4.2 Why L0 has no criteria

Level 0 describes what happens when there is no shared system: people paste
logs into a chatbot, and nothing is recorded. There is nothing to measure, so
there is nothing to test for. Every team starts at level 0 and stays there
until the L1 criteria hold. A team that meets none of the criteria above is at
level 0, including a team that has not connected any data yet.

### 4.3 Self-assessment

Organisational practices the data cannot show (on-call engineers trust AI
output, post-incident reviews include the AI's investigation, the policy is
reviewed at least quarterly, …) SHOULD be captured as a self-assessment and
reported **next to** the measured level, never in place of it.

## 5. Core practices

An implementation claiming a level MUST support the practices that level
relies on. Each practice produces the data one or more metrics are computed
from.

### 5.1 Read before write (L1+)
The agent gathers evidence with read actions before it concludes. Read actions
MAY run without approval. Every read action an agent runs SHOULD be recorded
in the investigation transcript.

### 5.2 Human-gated writes with verification after every change (L2+)
* Every write action MUST become an action proposal before it executes.
* Every proposal MUST carry a human-readable `reason`: why the action is
  needed, citing evidence, and what will happen.
* Implementations SHOULD attach a preview (dry run / blast radius) to the
  proposal.
* After **every** executed write — human-approved or automatic — the
  implementation MUST run a verification and record the result as
  `verified_resolved` on the proposal.

### 5.3 Autonomy policy as code (L3+)
* Whether a write is denied, approved by humans or run automatically MUST be
  decided by a versioned, machine-readable policy
  ([`autonomy-policy.schema.json`](autonomy-policy.schema.json)), not by the
  agent.
* Evaluation: rules are tried in order; the **first** rule whose `match`
  covers the write wins; no match → `defaults`. Match values are shell-style
  globs; an omitted match key matches everything.
* Modes: `deny` (refused; the agent is told why and no proposal is created),
  `approve` (needs `approvers` distinct humans), `auto` (runs immediately,
  then verified), `auto_after_trust` (`auto` once the rule has at least
  `trust.min_successes` verified successes and a failure rate at or below
  `trust.max_failure_rate` within `trust.window_days`; `approve` until then).
* Guardrails narrow `auto` back to `approve`, evaluated in this order:
  kill switch → external-agent rule (section 5.7) → tripped circuit breaker →
  change-freeze window → hourly automatic-action cap.
* An automatic action whose verification fails MUST trip the circuit breaker
  for its rule (when configured), demoting the rule to `approve` for the
  cooldown.
* A kill switch MUST exist that pauses every automatic action at once. Any
  authorised human SHOULD be able to turn it on; turning it off MAY be
  restricted to administrators.
* With no saved policy, the effective policy MUST be: every write needs one
  approval (Level 2 behaviour).
* Every policy change MUST be versioned and audited.

### 5.4 Investigation records and verdicts (L4)
* Each investigation MUST be recorded as an investigation record
  ([`investigation.schema.json`](investigation.schema.json)), including when
  it started, when it first named a root cause, the root cause text, the
  proposals it led to and how the incident was resolved.
* Humans grade the root cause with a **verdict**: `correct`, `partial` or
  `wrong`. Verdicts are the ground truth behind `rca_agreement_rate`.

### 5.5 Runbooks as code (L4)
* Operational knowledge MUST be stored as runbook files: YAML frontmatter
  ([`runbook.schema.json`](runbook.schema.json)) followed by a markdown body
  with `## Symptoms`, `## Diagnosis` and `## Fix` sections.
* A runbook applies to an alert when the alert's name is in
  `match.alertname` and every key in `match.labels` equals the alert's label.
  Published runbooks that apply MUST be given to the agent.
* After a verified fix with no applicable published runbook, an
  implementation SHOULD draft one from the investigation. Drafts MUST NOT be
  used until a human publishes them.

### 5.6 Eval suites (L4)
* Agent quality MUST be measured against reproducible scenarios
  ([`eval-scenario.schema.json`](eval-scenario.schema.json)): how to inject a
  fault, which alert to expect, and an answer key written before any model
  runs.
* Reference grading (weights): the root cause mentions an expected keyword
  40 %, a correct fix tool proposed 40 %, no forbidden tool used 10 %,
  diagnosed within the timeout 10 %. Implementations MAY use other weights but
  MUST publish them alongside scores.

### 5.7 An open tool protocol (L5)
* An implementation at L5 MUST let any external agent use it through an open
  protocol. The reference binding is the
  [Model Context Protocol](https://modelcontextprotocol.io) (version
  `2025-06-18`).
* External agents MAY run read actions. They MUST NOT execute writes
  directly: they submit action proposals, which go through the same policy.
* A rule whose mode resolves to `auto` MUST be downgraded to `approve` for
  proposals from an external agent unless the rule explicitly sets
  `allow_external_auto: true`.
* Every external call MUST be audited (kind `mcp.call`).

### 5.8 Audit chain (L5)
* Every AI read, proposal, denial, approval, rejection, execution,
  verification, policy change, verdict, runbook publication, external call and
  eval result MUST be written to an append-only audit log
  ([`audit-event.schema.json`](audit-event.schema.json)).
* The log MUST be hash-chained per tenant (or per organisation):
  `hash = sha256_hex(prev_hash + canonical_json(body))` where `body` is
  `{seq, kind, actor, subject, data, created_at}`, canonical JSON has sorted
  keys and `,` / `:` separators with no whitespace, and `created_at` is
  ISO-8601 in UTC. The first event has `seq = 1` and `prev_hash = ""`. `seq`
  increases by exactly 1.
* Writers MUST serialise so the chain cannot fork. A verifier walks the chain
  and reports the first sequence gap, `prev_hash` mismatch or hash mismatch.
* Core kinds: `tool.read`, `action.proposed`, `action.denied`,
  `action.approved`, `action.approval_recorded`, `action.rejected`,
  `action.executed`, `action.auto_executed`, `action.verified`,
  `policy.updated`, `policy.kill_switch`, `policy.breaker_tripped`,
  `investigation.verdict`, `runbook.published`, `runbook.drafted`, `mcp.call`,
  `eval.recorded`. Implementation-specific kinds MUST be prefixed `x-`.
* Actors: a human's email, `agent` for the built-in agent, `mcp:<email>` for
  an external agent acting with that person's credentials, `verifier` for the
  verification step.

## 6. Artefacts

All schemas are JSON Schema draft 2020-12 and live next to this file.
Examples of each artefact are in [`examples/`](examples/).

| § | Artefact | Schema | Example | Format on disk |
|---|---|---|---|---|
| 6.1 | Autonomy policy | [`autonomy-policy.schema.json`](autonomy-policy.schema.json) | [`examples/autonomy-policy.yaml`](examples/autonomy-policy.yaml) | YAML |
| 6.2 | Action proposal | [`action-proposal.schema.json`](action-proposal.schema.json) | [`examples/action-proposal.json`](examples/action-proposal.json) | JSON |
| 6.3 | Audit event | [`audit-event.schema.json`](audit-event.schema.json) | [`examples/audit-event.json`](examples/audit-event.json) | JSON (one per line in exports) |
| 6.4 | Investigation record | [`investigation.schema.json`](investigation.schema.json) | [`examples/investigation.json`](examples/investigation.json) | JSON |
| 6.5 | Runbook | [`runbook.schema.json`](runbook.schema.json) (frontmatter) | [`examples/runbook.md`](examples/runbook.md) | Markdown + YAML frontmatter |
| 6.6 | Eval scenario | [`eval-scenario.schema.json`](eval-scenario.schema.json) | [`examples/eval-scenario.yaml`](examples/eval-scenario.yaml) | YAML |

Notes:

* **Autonomy policy** — `version: 1` is the document format version. Tool
  names in `match.tools` are the implementation's write tools; the reference
  set is `rollout_undo`, `scale_deployment`, `restart_deployment`,
  `delete_pod`, `patch_resource`.
* **Action proposal** — `input` carries the tool arguments, including
  `reason`. `decision.mode` is the effective mode after guardrails;
  `decision.configured_mode` is what the rule says.
* **Audit event** — the example's `hash` is computed with the algorithm in
  section 5.8 and can be checked by any implementation.
* **Runbook** — `match` must name at least one alertname or label; the body
  is free markdown with the three sections above.

## 7. Versioning

* This document and its schemas share one version, `MAJOR.MINOR`
  (currently `0.1`), recorded in each schema as `x-aomm-version`.
* **Minor** versions only add: new optional fields, new audit kinds, new
  metrics, new practices marked SHOULD. A document valid under `0.x` stays
  valid under every later `0.y` with the same major version.
* **Major** versions may remove or rename fields, change a metric definition
  or change level criteria. Level claims MUST name the version they were
  measured against ("AOMM 0.1 Level 3").
* Before `1.0`, minor versions MAY tighten level thresholds; implementations
  SHOULD report the thresholds they applied.
* Artefact documents MAY carry extension fields only where a schema allows
  additional properties. Implementations SHOULD prefix extension audit kinds
  and extension keys with `x-`.
* Schema `$id`s are stable URLs; a new major version gets new `$id`s.

## 8. Conformance

An implementation conforms to AOMM 0.1 at level *N* when it supports the
practices in section 5 that levels 1..*N* rely on, computes the metrics in
section 3 with the definitions given (reporting the five TRACE metrics
together), and can import and export the
artefacts in section 6 in the published formats.

## 9. Reference implementation

[Daalu](https://github.com/kiwueke1/daalu) implements AOMM 0.1 end to end:
policy evaluation (`core/aiops/policy.py`), the audit chain
(`core/aiops/audit.py`), investigations and verdicts, runbooks as code, the
eval harness (`daalu eval`), metrics and automatic level scoring, and an MCP
server (`POST /api/v1/mcp`, `daalu mcp`). Its design notes are in
`docs/design/ai-ops-maturity.md`. The generated schemas in this directory are
produced by `scripts/gen_specs.py` from that implementation's models.
