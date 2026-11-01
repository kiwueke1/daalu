# The AI-Ops Maturity Model (AOMM), for your team

DevOps gave teams a shared picture of good delivery: practices written as
code, and four numbers (DORA) anyone could compute. The AI-Ops Maturity
Model does the same for how an infrastructure team works with AI. It asks
two questions: **who decides** what the AI may change, and **what the team
learns** from each incident.

This page is the short version for a team deciding where it stands and what
to do next. The standard itself is [`spec/README.md`](../spec/README.md)
(AOMM v0.1, with JSON Schemas for every artefact). Daalu's implementation is
documented in the
[engineering manual, Part XV](book-engineer/15-ai-ops-maturity/README.md)
and, page by page for operators, in the
[Operator's Handbook, Part IX](book-customer/09-ai-operations-maturity/README.md).

---

## The six levels

| Level | Name | What it looks like | You're here if |
|---|---|---|---|
| 0 | Ad hoc | Engineers paste logs into a chatbot. Nothing is shared or recorded. | The AI's work disappears when the browser tab closes. |
| 1 | Assisted | The AI reads alerts, logs, metrics and cluster state and explains them. Read-only. | Most alerts get an AI investigation that the whole team can see. |
| 2 | Proposing | The AI names the root cause and proposes a fix. Every change is approved by a human and verified afterwards. | Fixes go through an approve button, not a terminal on the side. |
| 3 | Trusted | A reviewed policy lets low-risk, proven actions run on their own. | Some fixes run without a human, under rules you wrote. |
| 4 | Learning | Every incident improves the next: verdicts on root causes, runbooks, evals, metrics. | You grade the AI's diagnoses and benchmark models before switching. |
| 5 | Autonomous | The AI resolves most routine incidents. Humans own the policy and the exceptions. | People get paged for what the policy can't handle. |

Levels are cumulative: you're at the highest level whose criteria, and
every lower level's, all hold. Daalu measures this from what actually
happened in the last 30 days (7 and 90 are also available), not from what is
switched on.

---

## How to tell where you are

Open **AI Ops → Maturity** in Daalu. The big number is computed from these
criteria:

| Level | All of these must hold over the window |
|---|---|
| 1 | At least 5 alerts reached Daalu, and the AI investigated at least 50 % of them. |
| 2 | At least one human-approved fix ran, and at least one proposal was approved or rejected. |
| 3 | Your saved autonomy policy has at least one `auto` or `auto_after_trust` rule, at least one automatic fix ran, and no more than 15 % of executed AI fixes failed verification. |
| 4 | At least 30 % of investigations have a human verdict, at least one runbook is published, and at least one eval ran in the window. |
| 5 | At least 50 % of resolved incidents were fixed automatically, root-cause agreement is at least 80 %, the audit chain verifies, and the saved policy has a circuit breaker and an hourly cap on automatic actions. |

Below the criteria, **Next steps** lists exactly what is missing for the
next level. Next to the computed level, a **self-assessment** of 16 yes/no
questions covers what data can't see: does on-call review the AI's root
cause before acting, does the policy go through code review, do
post-incident reviews include the AI's investigation, is there an error
budget for automatic changes. Answer it honestly; it is reported beside the
measured level, never instead of it.

---

## The metrics

The AOMM equivalent of DORA. All are rates from 0 to 100 % over the window,
or "—" when there is nothing to measure yet.

| Metric | Question it answers | Healthy direction |
|---|---|---|
| AI triage rate | What share of alerts does the AI investigate? | Up; ≥ 50 % for Level 1 |
| Time to diagnosis | How long from the alert firing to the AI naming a root cause? | Down |
| RCA agreement | How often does the team agree with the AI's root cause (partly right counts half)? | Up; ≥ 80 % for Level 5 |
| Verdict coverage | What share of investigations has a human verdict? | ≥ 30 % for Level 4 |
| Approval rate | Of the fixes put to a human, how many were approved? | Up, but not 100 %: a team that never rejects isn't reading |
| AI change-failure rate | Of the AI fixes that ran, how many didn't fix the problem? | Down; ≤ 15 % for Level 3 |
| Auto-resolution rate | Of resolved incidents, how many did the AI fix with no human? | Up; ≥ 50 % for Level 5 |
| Time to resolve | Alert firing to verified fix (or resolution) | Down |

Engineering detail on each definition:
[ch. 88](book-engineer/15-ai-ops-maturity/88-metrics-and-maturity-scoring.md).
For operators: [ch. 55](book-customer/09-ai-operations-maturity/55-metrics-dashboard.md).

---

## The practices, as code

| Practice | Artefact | In Daalu |
|---|---|---|
| Autonomy policy | YAML: which writes are denied, need N approvers, or run automatically; guardrails (kill switch, circuit breaker, freeze windows, hourly cap) | **AI Ops → Autonomy policy** |
| Investigation record | One per alert: root cause, actions, outcome, verdict | **AI Ops → Investigations** |
| Runbooks | Markdown with frontmatter, matched to alerts, injected into the AI's triage | **AI Ops → Runbooks**, `daalu runbooks import` |
| Evals | YAML scenarios: break something, grade the AI against an answer key | **AI Ops → Evals**, `daalu eval` ([`evals/`](../evals/README.md)) |
| Audit chain | Append-only, hash-chained log of every read, proposal, approval and policy change | **AI Ops → Audit log** |
| Open tool protocol | Any agent uses the same tools under the same policy | MCP: [`docs/mcp.md`](mcp.md) |

With no saved policy, Daalu behaves exactly as before AOMM: every change the
AI proposes waits for one human approval.

---

## A 90-day climb

A realistic path for a team starting with Daalu connected to its clusters
and alerts flowing in.

**Days 1–14: Level 1 → 2.**

* Autonomy policy → **Starter policies… → Level 2 — every change
  approved** → save. This turns on automatic investigation of every new
  alert and denies writes in `kube-system`.
* Have on-call open each AI investigation before acting and use the approve
  card for the fix instead of a terminal. Reject with a reason when the AI
  is wrong.
* Check **Maturity**: triage rate above 50 %, one approved fix executed.

**Days 15–45: build the track record, start learning.**

* Grade root causes on **Investigations → Needs a verdict** in the weekly
  on-call handover. Aim for a third of investigations.
* Publish your three most common runbooks (import your existing markdown
  with `daalu runbooks import`, or publish the drafts the AI writes after a
  verified fix).
* Run the eval suite once against a test cluster, labelled with your current
  model.
* Add `auto_after_trust` rules for the two or three fixes you approve most
  (restarts and scaling in non-production are typical). They keep needing
  approval until each has 10 verified successes with a low failure rate, so
  adding them early costs nothing.

**Days 46–90: Level 3 → 4, aim for 5.**

* Watch **Autonomy policy → Trust by rule** as rules earn automatic
  execution. Make sure everyone on call knows the **Pause all automation**
  kill switch; anyone can press it.
* Keep the AI change-failure rate under 15 %. When the circuit breaker
  trips, read the failed fix, narrow the rule, then reset it.
* Re-run evals before any model change and compare labels on the
  leaderboard.
* Review the policy, the metrics and a sample of the audit log once a
  quarter. Extend `auto_after_trust` to your most frequent proven fixes in
  production, with a freeze window for your riskiest hours.

Level 5 isn't a target date; it is what the numbers look like once most
routine incidents are fixed by rules that earned it, and the humans spend
their time on the policy and the exceptions.

---

## Further reading

* The standard: [`spec/README.md`](../spec/README.md)
* Engineering manual: [Part XV — The AI-Ops Maturity Model](book-engineer/15-ai-ops-maturity/README.md)
* Operator's Handbook: [Part IX — AI Operations Maturity](book-customer/09-ai-operations-maturity/README.md)
* Connect Claude Code or Cursor: [`docs/mcp.md`](mcp.md)
* Eval scenarios: [`evals/README.md`](../evals/README.md)
* Design contract: [`docs/design/ai-ops-maturity.md`](design/ai-ops-maturity.md)
