# The AI-Ops Maturity Model (AOMM), for your team

DevOps gave teams four numbers (DORA) anyone could compute, and practices
written as code. The AI-Ops Maturity Model does the same for how an
infrastructure team works with AI. It starts with **five TRACE metrics** that
say how well the AI is doing, then uses thresholds on those metrics to place
a team on **six levels**, so the level is measured, never claimed.

This page is the short version for a team deciding where it stands and what
to do next: the metrics first, then how they map to the levels. The standard itself is [`spec/README.md`](../spec/README.md)
(AOMM v0.1, with JSON Schemas for every artefact). Daalu's implementation is
documented in the
[engineering manual, Part XV](book-engineer/15-ai-ops-maturity/README.md)
and, page by page for operators, in the
[User Guide, Part IX](user-guide/09-ai-operations-maturity/README.md).

---

## The metrics

### The five TRACE metrics

These are the AOMM equivalent of the DORA four keys. DORA measures delivery
with two speed numbers and two stability numbers; AOMM measures AI operations
with two speed numbers, two quality numbers, and one for autonomy.
**TRACE** is their initials: Time to **T**riage, time to **R**esolve, **A**greement rate, **C**hange-failure rate, **E**scalation rate.

| Group | Metric | Question it answers | Better is | DORA counterpart |
|---|---|---|---|---|
| Speed | Time to triage | How long from the alert firing to the AI naming a root cause? | Lower | Lead time for changes |
| Speed | Time to resolve | How long from the alert firing to a verified fix (or resolution)? | Lower | Time to restore service |
| Quality | Agreement rate | How often does the team agree with the AI's root cause (partly right counts half)? | Higher | — (is the AI right?) |
| Quality | Change-failure rate | Of the AI fixes that ran, how many didn't fix the problem? | Lower | Change-failure rate |
| Autonomy | Escalation rate | Of resolved incidents, how many still needed a human? | Lower | — (new to AI operations) |

Read them together. A fast diagnosis that is usually wrong is worse than a
slow one that is right, and a low escalation rate with a high change-failure rate is
unattended damage. A healthy team sees the two times fall while agreement
stays high and change failures stay low, with escalation rate falling on top.

### Supporting metrics

These explain the TRACE metrics and show whether the practices behind them are
in place.

| Metric | Question it answers | Healthy direction |
|---|---|---|
| Alerts in the window | Is real alert traffic reaching the AI? | Enough to measure: ≥ 5 |
| AI triage rate | What share of alerts does the AI investigate? | Up |
| Verdict coverage | What share of investigations has a human verdict? (The sample behind agreement rate.) | Up |
| Approval rate | Of the fixes put to a human, how many were approved? | Up, but not 100 %: a team that never rejects isn't reading |
| Runbooks published / drafted | Is knowledge being kept as code? | Up |
| Eval runs and mean score | Is the AI benchmarked before models change? | Regular runs; score up |
| Audit chain | Can the record of what the AI did be trusted? | Always verifies |

Rates run from 0 to 100 % over the window (30 days by default; 7 and 90 are
also available), or "—" when there is nothing to measure yet.

Engineering detail on each definition:
[ch. 88](book-engineer/15-ai-ops-maturity/88-metrics-and-maturity-scoring.md).
For operators: [ch. 55](user-guide/09-ai-operations-maturity/55-metrics-dashboard.md).

---

## How the metrics map to the levels

### The six levels

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
happened in the window, not from what is switched on.

### The thresholds

Each level is a set of thresholds on the metrics above, plus a few facts
about what ran and what the saved policy allows:

| Level | All of these must hold over the window |
|---|---|
| 1 | Alerts in the window ≥ 5, and AI triage rate ≥ 50 %. |
| 2 | At least one human-approved fix ran, and the approval rate is measured (at least one proposal approved or rejected). |
| 3 | Change-failure rate ≤ 15 %; your saved autonomy policy has at least one `auto` or `auto_after_trust` rule, and at least one automatic fix ran. |
| 4 | Verdict coverage ≥ 30 %, at least one runbook published, and at least one eval run in the window. |
| 5 | Escalation rate ≤ 50 %, agreement rate ≥ 80 %, the audit chain verifies, and the saved policy has a circuit breaker and an hourly cap on automatic actions. |

Read from the metric side, three of the five TRACE metrics gate a level:
**Change-failure rate** gates Level 3, and **escalation rate** and
**Agreement rate** gate Level 5. **Time to triage** and **time to resolve**
gate nothing, on purpose: every team's systems and alert mix differ, so
there is no fair single threshold. They are what climbing the levels is meant
to improve; compare them against your own history.

---

## How to tell where you are

Open **Insights → Maturity** in Daalu. The headline tiles show the metrics for
the window; the big number is the level those metrics put you at. Below
them, every criterion is listed with its current value and its target.

Under the criteria, **Next steps** lists exactly what is missing for the
next level. Next to the computed level, a **self-assessment** of 16 yes/no
questions covers what data can't see: does on-call review the AI's root
cause before acting, does the policy go through code review, do
post-incident reviews include the AI's investigation, is there an error
budget for automatic changes. Answer it honestly; it is reported beside the
measured level, never instead of it.

---

## The practices, as code

| Practice | Artefact | In Daalu |
|---|---|---|
| Autonomy policy | YAML: which writes are denied, need N approvers, or run automatically; guardrails (kill switch, circuit breaker, freeze windows, hourly cap) | **Automate → Autonomy policy** |
| Investigation record | One per alert: root cause, actions, outcome, verdict | **Respond → Investigations** |
| Runbooks | Markdown with frontmatter, matched to alerts, injected into the AI's triage | **Automate → Runbooks**, `daalu runbooks import` |
| Evals | YAML scenarios: break something, grade the AI against an answer key | **Insights → Evals**, `daalu eval` ([`evals/`](../evals/README.md)) |
| Audit chain | Append-only, hash-chained log of every read, proposal, approval and policy change | **Insights → Audit log** |
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
* Keep the change-failure rate under 15 %. When the circuit breaker
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
* User Guide: [Part IX — AI Operations Maturity](user-guide/09-ai-operations-maturity/README.md)
* Connect Claude Code or Cursor: [`docs/mcp.md`](mcp.md)
* Eval scenarios: [`evals/README.md`](../evals/README.md)
* Design contract: [`docs/design/ai-ops-maturity.md`](design/ai-ops-maturity.md)
