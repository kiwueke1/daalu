# 81. The AI-Ops Maturity Model

Daalu's goal is to define how infrastructure teams integrate AI, the way
DevOps defined how they ship software. DevOps won because it came with three
things at once: a picture of what good looks like, practices you can express
as code, and shared metrics (DORA) that let a team say where it stands. The
AI-Ops Maturity Model (AOMM) is the same package for AI in operations. This
chapter presents the model, explains how Daalu computes a team's level, and
maps every Daalu feature, including the ones that predate AOMM, to the level
it serves.

The normative text is [`spec/README.md`](../../../spec/README.md) (AOMM v0.1).
The build contract is [`docs/design/ai-ops-maturity.md`](../../design/ai-ops-maturity.md).
The scoring code is `src/daalu_automation/core/aiops/maturity.py`.

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

## How the level is computed

`maturity.compute` (`core/aiops/maturity.py:254`) reads the metrics for the
window (`metrics.collect`, chapter 88), a few facts about the *saved* policy
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
| 5 | `auto_resolution_rate` | `auto_resolution_rate ≥ 0.5` |
| 5 | `rca_agreement_rate` | `rca_agreement_rate ≥ 0.8` |
| 5 | `audit_chain_ok` | the audit chain verifies |
| 5 | `guardrails` | the saved policy has a `circuit_breaker` and a non-zero `max_auto_actions_per_hour` |

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

## Mapping Daalu to the levels

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
* Chapter 88 — the exact metric definitions behind the criteria above.
* Team-facing summary with a 90-day plan: [`docs/maturity-model.md`](../../maturity-model.md).
