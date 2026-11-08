# Part IX — AI Operations Maturity

DevOps spread because it came with a handful of shared numbers (the DORA
metrics) that told a team where it stood, and practices you could write down
as code. The **AI-Ops Maturity Model (AOMM)** does the same for how an
infrastructure team works with AI. It starts with **five TRACE metrics** that
Daalu measures for you from what actually happened, and uses thresholds on
those metrics to place your team on **six levels**. Practices you keep as code
(an autonomy policy, runbooks, eval scenarios) are how you move the numbers.

This part of the handbook is for the operators who use it day to day. Every
page it describes lives under the **AI Ops** section of the sidebar:
**Maturity**, **Autonomy policy**, **Investigations**, **Runbooks**,
**Evals** and **Audit log**.

---

## The five TRACE metrics

DORA measures delivery with two speed numbers and two stability numbers.
AOMM measures AI operations with two speed numbers, two quality numbers and
one for autonomy. **TRACE** is their initials: Time to **T**riage, time to **R**esolve, **A**greement rate, **C**hange-failure rate, **E**scalation rate.

| Group | Metric | Question it answers | Better is |
|---|---|---|---|
| Speed | **Time to triage** | How long from the alert firing to the AI naming a root cause? | Lower |
| Speed | **Time to resolve** (MTTR) | How long from the alert firing to the incident being resolved? | Lower |
| Quality | **Agreement rate** | How often does your team agree with the AI's root cause? | Higher |
| Quality | **Change-failure rate** | Of the AI fixes that ran, how many failed verification? | Lower |
| Autonomy | **Escalation rate** | Of resolved incidents, how many still needed a human? | Lower |

Read them together: a fast diagnosis that is usually wrong doesn't help, and
automatic fixes with a high failure rate are unattended damage. Supporting
metrics (triage rate, verdict coverage, approval rate, runbooks, evals, the
audit chain) explain the key five. Chapter 55 covers every number.

---

## How the metrics map to the six levels

| Level | Name | What it looks like | Measured by | What Daalu gives you |
|---|---|---|---|---|
| 0 | Ad hoc | People paste logs into a chatbot. No shared context, no record. | — (the starting point) | — |
| 1 | Assisted | The AI reads alerts, logs and metrics and explains them. Read-only. | Alerts in the window, AI triage rate | Automatic investigation of new alerts, read-only investigation tools, morning briefings |
| 2 | Proposing | The AI names the root cause and proposes a fix; every change is approved by a human. | A human-approved fix ran; approval rate measured | Approve / reject cards on the alert, verification after every change |
| 3 | Trusted | A policy lets low-risk, proven actions run on their own. | **Change-failure rate**; an automatic rule and an automatic fix | The autonomy policy, trust thresholds, previews, multiple approvers |
| 4 | Learning | Every incident improves the next one. | Verdict coverage, published runbooks, eval runs | Investigations and verdicts, runbooks, evals, the metrics dashboard |
| 5 | Autonomous | The AI resolves most routine incidents; humans own the policy and the exceptions. | **Escalation rate**, **agreement rate**, audit chain, guardrails | Guardrails (kill switch, circuit breaker, freeze windows, hourly cap), the audit log, MCP for any agent |

Levels are cumulative: you are at the highest level whose criteria, and every
lower level's criteria, are all met. Time to triage and time to resolve
gate no level, on purpose: every team's systems and alert mix differ, so
there's no fair single threshold. They are what climbing the levels should
improve; compare them with your own history. Chapter 51 lists every
threshold.

A tenant that has never saved a policy behaves exactly like Daalu did before
AOMM: every change the AI proposes waits for one human approval.

---

## Chapters

| # | Chapter | What it covers |
|---|---------|----------------|
| 51 | [Where is your team?](51-where-is-your-team.md) | The five TRACE metrics, how their thresholds set your level, the **Maturity** page and the self-assessment. |
| 52 | [Setting your autonomy policy](52-setting-your-autonomy-policy.md) | Writing, testing and saving a policy; the three starter policies; the kill switch; what to do when the circuit breaker trips. |
| 53 | [Investigations and verdicts](53-investigations-and-verdicts.md) | The **Investigations** page, what each resolution means, and grading the AI's root cause. |
| 54 | [Runbooks](54-runbooks.md) | The runbook file format, how runbooks match alerts, publishing AI-drafted runbooks, importing a folder. |
| 55 | [The metrics dashboard](55-metrics-dashboard.md) | Every metric in detail: the five TRACE metrics, the supporting ones, what a healthy value is and which level each one gates. |
| 56 | [The audit log](56-audit-log.md) | Reading, filtering and verifying the hash-chained log of everything the AI and your team did. |
| 57 | [Running evals](57-running-evals.md) | Benchmarking your AI SRE on scripted outages, comparing models, reading the leaderboard. |
| 58 | [Connect your agent with MCP](58-connect-your-agent-with-mcp.md) | Using Claude Code, Cursor or any MCP client against Daalu: `daalu login`, then `daalu mcp`. |

A suggested order: read 51 for the metrics and where they put you, keep 55
open for the detail on any number, set a policy with 52, then work through
53, 54 and 56 as your team starts grading investigations and publishing
runbooks.
57 and 58 are for when you want to compare models or bring your own agent.

---

## Related reading

- The team-facing summary of the model and a 90-day plan:
  [docs/maturity-model.md](../../maturity-model.md)
- The open standard text and JSON Schemas: [spec/README.md](../../../spec/README.md)
- MCP connection reference: [docs/mcp.md](../../mcp.md)
- How it's built (engineering manual, Part XV):
  [book-engineer/15-ai-ops-maturity](../../book-engineer/15-ai-ops-maturity/README.md)
- The alert page these features hang off:
  [Chapter 20 — Alerts and incident response](../04-feature-walkthroughs/20-alerts-and-incidents.md)
