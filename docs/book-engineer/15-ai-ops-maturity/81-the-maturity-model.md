# 81. The AI-Ops Maturity Model

Daalu's goal is to define how infrastructure teams integrate AI, the way
DevOps defined how they ship software. DevOps won because it came with three
things at once: shared metrics (DORA) that let a team say where it stands, a
picture of what good looks like, and practices you can express as code. The
AI-Ops Maturity Model (AOMM) is the same package for AI in operations, and it
is built the same way round: metrics first, levels derived from them. This
chapter presents the metrics, shows how they map to the six levels, explains
how Daalu computes a team's level, and maps every Daalu feature, including the
ones that predate AOMM, to the level it serves.

The normative text is [`spec/README.md`](../../../spec/README.md) (AOMM v0.1).
The build contract is [`docs/design/ai-ops-maturity.md`](../../design/ai-ops-maturity.md).
The scoring code is `src/daalu_automation/core/aiops/maturity.py`.

---

## The metrics

DORA measures delivery with two speed numbers (deployment frequency, lead
time) and two stability numbers (change-failure rate, time to restore). AOMM
measures AI operations with two speed numbers, two quality numbers, and one
for autonomy, which DORA has no need for. Exact computations are in
chapter 88.

### The five TRACE metrics

**TRACE** is their initials: Time to **T**riage, time to **R**esolve, **A**greement rate, **C**hange-failure rate, **E**scalation rate.

| Group | TRACE metric | Question it answers | Better is | API field | DORA counterpart |
|---|---|---|---|---|---|
| Speed | Time to **T**riage | How long from the alert firing to the AI naming a root cause? | Lower | `median_time_to_diagnosis_s` | Lead time for changes |
| Speed | Time to **R**esolve | How long from the alert firing to the incident being resolved? | Lower | `median_time_to_resolve_s` | Time to restore service |
| Quality | **A**greement rate | How often do humans agree with the AI's root cause (`partial` counts half)? | Higher | `rca_agreement_rate` | — (is the AI right?) |
| Quality | **C**hange-failure rate | Of the AI writes that ran, how many failed verification? | Lower | `ai_change_failure_rate` | Change-failure rate |
| Autonomy | **E**scalation rate | Of resolved incidents, how many still needed a human? | Lower | `escalation_rate` | — (new to AI operations) |

The API field names predate the TRACE names and are kept so existing
clients keep working; the rest of this part uses the TRACE names in prose
and the field names where it quotes code.

They are read together, and that is the point of having five rather than
one. A fast time to triage with low agreement rate is a fast wrong answer;
a low escalation rate with a high change-failure rate is unattended
damage. A team is doing well when the two times fall while agreement stays
high and change failures stay low, with escalation rate falling on top. The
Maturity page shows all five as headline tiles (as "Time to triage",
"Agreement rate", "Change-failure rate", "Escalation rate" and
"MTTR"), next to three supporting ones.

### Supporting metrics

| Group | Metric | Why it matters |
|---|---|---|
| Coverage | `alerts_total`, `ai_triage_rate` | How much of the incident work the AI sees and investigates. |
| Feedback | `verdict_coverage` | How much of the AI's work humans grade; the sample behind agreement rate (`rca_agreement_rate`). |
| Trust | `proposal_approval_rate` | How often humans accept the AI's proposed writes. |
| Learning | `runbooks_published`, `runbooks_generated`, `eval_runs`, `eval_mean_score` | Whether each incident improves the next. |
| Integrity | `audit_chain_ok` | Whether the record of what the AI did can be trusted. |

Some supporting metrics are inputs to the TRACE ones (no verdicts, no
agreement rate); the rest count whether a practice is in use. Of them,
`ai_triage_rate`, `proposal_approval_rate` and `audit_chain_ok` are also
tiles on the Maturity page.

---

## The six levels

| Level | Name | Definition (`core/aiops/maturity.py:26`) |
|---|---|---|
| 0 | Ad hoc | Individuals paste logs into a chatbot. No shared context, no record. |
| 1 | Assisted | AI reads alerts, logs, metrics and explains them. Read-only. |
| 2 | Proposing | AI names the root cause and proposes a fix; every change is human-approved. |
| 3 | Trusted | Policy lets low-risk, proven actions run automatically. |
| 4 | Learning | Every incident improves the next: verdicts, runbooks, evals, metrics. |
| 5 | Autonomous | AI resolves most routine incidents; humans own policy and exceptions. |

The levels describe **who decides** and **what the organisation learns**,
not how clever the model is. A team running the best frontier model with
engineers pasting logs into a chat window is at Level 0: nothing is shared,
nothing is recorded, nothing compounds. A team on a modest self-hosted model
whose every alert is investigated, whose fixes go through a recorded approval
and verification, and whose low-risk fixes run under a reviewed policy is at
Level 3.

Levels are **cumulative**. A team is at the highest level whose criteria
*and every lower level's criteria* hold over the measurement window. Meeting
the Level 4 criteria without the Level 3 ones leaves a team at Level 2
(`level_from_criteria`, `core/aiops/maturity.py:171`). The reason: a learning
loop around a process nobody trusts to run on its own is not more mature than
a trusted one, it is a different shape, and a ladder that can be climbed out
of order stops being a ladder.

---

## How the metrics map to the levels

Each level is a set of thresholds on the metrics above, plus a few facts
about what ran and what the saved policy allows. `maturity.compute`
(`core/aiops/maturity.py:254`) reads the metrics for the window
(`metrics.collect`, chapter 88), a few facts about the *saved* policy
(`_policy_facts`, `core/aiops/maturity.py:182`), and evaluates fourteen
criteria (`build_criteria`, `core/aiops/maturity.py:103`):

| Level | Criterion id | Met when |
|---|---|---|
| 1 | `triage_rate` | `ai_triage_rate ≥ 0.5` |
| 1 | `alert_volume` | `alerts_total ≥ 5` in the window |
| 2 | `human_approved_executed` | at least one human-approved write executed |
| 2 | `approval_rate_known` | `proposal_approval_rate` is not null (at least one approve or reject) |
| 3 | `auto_rule` | the saved policy has ≥ 1 rule in `auto` / `auto_after_trust` (a `defaults.mode` of `auto`/`auto_after_trust` counts as one) |
| 3 | `auto_executed` | at least one automatic write executed |
| 3 | `change_failure_rate` | `ai_change_failure_rate ≤ 0.15` |
| 4 | `verdict_coverage` | `verdict_coverage ≥ 0.3` |
| 4 | `runbooks_published` | ≥ 1 published runbook |
| 4 | `eval_runs` | ≥ 1 eval run started in the window |
| 5 | `escalation_rate` | `escalation_rate ≤ 0.5` |
| 5 | `rca_agreement_rate` | `rca_agreement_rate ≥ 0.8` |
| 5 | `audit_chain_ok` | the audit chain verifies |
| 5 | `guardrails` | the saved policy has a `circuit_breaker` and a non-zero `max_auto_actions_per_hour` |

Read from the metric side:

| Metric | Kind | Gates | Threshold |
|---|---|---|---|
| Time to triage (`median_time_to_diagnosis_s`) | TRACE | — | none |
| Time to resolve (`median_time_to_resolve_s`) | TRACE | — | none |
| Change-failure rate (`ai_change_failure_rate`) | TRACE | L3 | ≤ 0.15 |
| Escalation rate (`escalation_rate`) | TRACE | L5 | ≤ 0.5 |
| Agreement rate (`rca_agreement_rate`) | TRACE | L5 | ≥ 0.8 |
| `alerts_total` | Supporting | L1 | ≥ 5 |
| `ai_triage_rate` | Supporting | L1 | ≥ 0.5 |
| `proposal_approval_rate` | Supporting | L2 | not null |
| `verdict_coverage` | Supporting | L4 | ≥ 0.3 |
| `runbooks_published` | Supporting | L4 | ≥ 1 |
| `eval_runs` | Supporting | L4 | ≥ 1 |
| `audit_chain_ok` | Supporting | L5 | true |

The other four criteria are not metrics: `human_approved_executed` and
`auto_executed` are counts `collect` returns outside the public metrics shape
(its `extras`), and `auto_rule` and `guardrails` are facts about the saved
policy.

**The speed metrics gate nothing, on purpose.** Time to triage and time
to resolve depend on a team's systems and alert mix: a fleet of stateless
web services and a GPU cluster with slow-draining nodes should not share a
threshold. They are the outcome climbing the levels is meant to improve, so
the Maturity page shows them as tiles, but no criterion tests them. Compare them against your own history.

Two details worth knowing:

* **Policy facts come from the saved policy only.** With no saved row,
  `_policy_facts` returns `auto_rules: 0, guardrails: False` even though the
  built-in default YAML (`core/aiops/policy.py:158`) contains a breaker and
  a cap. The built-in default is "nothing runs automatically"; claiming
  guardrails for it would credit a team for a decision it never made.
* **The headline is automatic, the self-assessment sits beside it.** Sixteen
  yes/no questions (`QUESTIONS`, `core/aiops/maturity.py:42`) cover what data
  can't see (on-call reviews the RCA, the policy is changed through code
  review, post-incident reviews include the AI, there is an error budget for
  automatic changes, ...). The self-assessed level uses the same cumulative
  rule (`self_assessed_level`, `core/aiops/maturity.py:84`). The UI shows it
  in a box next to the big number, never instead of it.

We considered a weighted score (0–100) instead of discrete levels and
rejected it: a score invites optimising whichever term is cheapest, and
"we're a 63" tells a team nothing about what to do next. Discrete levels with
named, binary criteria produce a to-do list. That list is `next_steps`
(`core/aiops/maturity.py:243`): the unmet criteria of the next level, then of
the level after, each with a sentence that names the page to open.

---

## Mapping Daalu features to the levels

AOMM was designed around what Daalu already did, then extended. Most of
Levels 1 and 2 existed before the AOMM branch; Levels 3–5 are new.

### Level 1 — Assisted (read before write)

| Feature | Where | Notes |
|---|---|---|
| Read-only kube tools (`get_pod_logs`, `describe_pod`, `get_pod_events`, `list_pods`, `get_deployment`, `rollout_history`) | `core/kube_tools.py` | Pre-existing. Auto-execute in the alert chat; `requires_approval=False`. |
| Prometheus / Loki / GET `call_external_api` / cloud read tools | `core/kube_tools.py`, `core/cloud_{aws,gcp,azure}.py` | Pre-existing. Cloud tools are read-only in v1. |
| `recent_changes` read tool | `core/aiops/change_context.py`, `core/kube_tools.py:531` | New. "What changed?" in one call (chapter 86). Also in the copilot allowlist (`api/routers/copilot.py:63`). |
| Infra agent triage on every alert event | `modules/infra/agent.py` | Pre-existing: writes a likely root cause into the alert body. |
| Automatic investigation | `modules/infra/agent.py:36`, `api/routers/alert_chat.py:1222` | New. With `investigate_automatically: true` the full alert-chat triage starts on every new alert (chapter 84). |
| Daily briefings | `core/briefings.py`, `api/routers/briefings.py` | Pre-existing. AI-written operational summaries. |
| Cluster federation | Part V | Pre-existing. The read tools reach the tenant's own clusters through the WireGuard hub and `daalu-edge`, so the AI reads the same state on-call does. |
| Local-first LLM router | `core/llm.py`, [ch. 31](../06-gpu-and-llm-serving/31-llm-router.md) | Pre-existing. Investigations can run on the customer's own GPU (the SOVEREIGN tier), so "send prod logs to a model" does not have to mean "send them to a third party". This is what makes Level 1 adoptable in regulated shops. |
| Investigation record | `core/aiops/investigations.py` | New. A shared, recorded investigation per alert is what separates Level 1 from Level 0 (chapter 84). |

### Level 2 — Proposing (human-gated writes, verified)

| Feature | Where | Notes |
|---|---|---|
| Pending `AlertAction` + approve / reject | `api/routers/alert_chat.py:1262`, `:1549` | Pre-existing. Every write lands as a card; nothing runs until a human clicks. |
| Execute-then-verify | `_run_approved_action`, `api/routers/alert_chat.py:1405` | Pre-existing. Runs the approved call verbatim, waits for the rollout, re-reads the deployment and pods, and re-engages the agent if the workload did not recover. |
| Write argument validation | `kube_tools.validate_write_input` | Pre-existing. A malformed write goes back to the model instead of becoming a card. |
| Remediation workflow runs | `WorkflowRun` rows with `module="remediation"` | Pre-existing. The approve button opens a run page that fills in step by step. |
| SoT change proposals | [ch. 16](../04-source-of-truth/16-change-proposals.md) | Pre-existing. The network-side equivalent of the approve card. |
| Policy decision + preview on the approve card | `frontend/components/aiops/action-policy.tsx` | New (chapters 82, 86). |

### Level 3 — Trusted (policy as code)

| Feature | Where |
|---|---|
| Autonomy policy (`deny` / `approve` / `auto` / `auto_after_trust`) | `core/aiops/policy.py` (chapter 82) |
| Trust thresholds per rule | `policy.trust_stats` (chapter 83) |
| Multi-approver | `api/routers/alert_chat.py:1292` (chapter 83) |
| Dry-run / blast-radius previews | `core/aiops/preview.py` (chapter 86) |
| Policy simulator | `POST /aiops/policy/simulate` (chapter 82) |

### Level 4 — Learning

| Feature | Where |
|---|---|
| Investigations, outcome verification, verdicts | chapter 84 |
| Runbooks as code, AI-drafted runbooks | chapter 85 |
| Eval harness and leaderboard | chapter 89 |
| Metrics | chapter 88 |

### Level 5 — Autonomous

| Feature | Where |
|---|---|
| Automatic execution with guardrails (kill switch, circuit breaker, freeze windows, hourly cap) | chapter 83 |
| Hash-chained audit log | chapter 87 |
| Exception queue: the Investigations page filtered to `unresolved` / "Needs a verdict", plus the Trust table's tripped breakers | chapters 84, 83 |
| MCP server: any agent, same tools, same policy | chapter 90 |

---

## Why the model is defined this way

**Why metrics first, levels second.** DORA's lesson is that outcomes are
what matter and that a ladder of practices, on its own, rewards box-ticking.
So the five TRACE metrics are the result AOMM asks a team to improve, and the
levels are a reading of those metrics plus the practices that produce them.
A team that asks "what level are we?" gets a number; a team that asks "are
we getting better?" looks at the TRACE metrics over time.

**Why levels at all, rather than a feature checklist.** A checklist
rewards buying features. Levels describe a working relationship between
people and the AI, and every criterion is measured from what actually
happened in the window, not from what is configured. A tenant can enable
every feature and still be Level 0 if no alerts flow in.

**Why Level 2 requires "every change approved" rather than "AI fixes
things".** Because the first thing that has to be earned is the record:
proposals, approvals, rejections and verification outcomes. Without that
history there is nothing to base a Level 3 policy on. `auto_after_trust`
(chapter 83) makes the dependency literal: a rule earns automatic execution
from its own verified track record at Level 2.

**Why Level 4 comes after Level 3.** We argued about this ordering. The
case for "Learning before Trusted" is that you want verdicts and evals before
anything runs on its own. The case we took: Level 3 is bounded by its own
guardrails (narrow rules, trust thresholds, breaker, kill switch), and the
learning loop is most valuable once there is automatic behaviour whose
quality has to be watched. The verdict, runbook and eval features all work
at Level 2 too; nothing stops a team doing them early, it just isn't what
the next criterion asks for.

**Why the default policy equals the pre-AOMM behaviour.** Upgrading Daalu
must never change what runs in a customer's cluster. With no saved policy,
`decide` returns `approve` with one approver for every write
(`core/aiops/policy.py:158`), which is exactly how the alert chat behaved
before. Climbing a level is always an explicit, audited policy change.

---

## Where to go next

* Chapter 82 — the policy that makes Level 3 possible.
* Chapter 88 — the exact metric computations behind the tables above.
* Team-facing summary with a 90-day plan: [`docs/maturity-model.md`](../../maturity-model.md).
