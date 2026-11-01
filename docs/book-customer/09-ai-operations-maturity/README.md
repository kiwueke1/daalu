# Part IX — AI Operations Maturity

DevOps spread because it came with a picture of what good looks like,
practices you could write down as code, and a handful of shared numbers
(the DORA metrics) that told a team where it stood. The **AI-Ops Maturity
Model (AOMM)** is the same thing for how an infrastructure team works with
AI: six levels, practices you keep as code (an autonomy policy, runbooks,
eval scenarios), and metrics Daalu measures for you from what actually
happened.

This part of the handbook is for the operators who use it day to day. Every
page it describes lives under the **AI Ops** section of the sidebar:
**Maturity**, **Autonomy policy**, **Investigations**, **Runbooks**,
**Evals** and **Audit log**.

---

## The six levels

| Level | Name | What it looks like | What Daalu gives you |
|---|---|---|---|
| 0 | Ad hoc | People paste logs into a chatbot. No shared context, no record. | — |
| 1 | Assisted | The AI reads alerts, logs and metrics and explains them. Read-only. | Automatic investigation of new alerts, read-only investigation tools, morning briefings |
| 2 | Proposing | The AI names the root cause and proposes a fix; every change is approved by a human. | Approve / reject cards on the alert, verification after every change |
| 3 | Trusted | A policy lets low-risk, proven actions run on their own. | The autonomy policy, trust thresholds, previews, multiple approvers |
| 4 | Learning | Every incident improves the next one. | Investigations and verdicts, runbooks, evals, the metrics dashboard |
| 5 | Autonomous | The AI resolves most routine incidents; humans own the policy and the exceptions. | Guardrails (kill switch, circuit breaker, freeze windows, hourly cap), the audit log, MCP for any agent |

Levels are cumulative: you are at the highest level whose criteria, and every
lower level's criteria, are all met. Chapter 51 shows exactly how that is
measured.

A tenant that has never saved a policy behaves exactly like Daalu did before
AOMM: every change the AI proposes waits for one human approval.

---

## Chapters

| # | Chapter | What it covers |
|---|---------|----------------|
| 51 | [Where is your team?](51-where-is-your-team.md) | The levels, the criteria behind your automatic level, the **Maturity** page and the self-assessment. |
| 52 | [Setting your autonomy policy](52-setting-your-autonomy-policy.md) | Writing, testing and saving a policy; the three starter policies; the kill switch; what to do when the circuit breaker trips. |
| 53 | [Investigations and verdicts](53-investigations-and-verdicts.md) | The **Investigations** page, what each resolution means, and grading the AI's root cause. |
| 54 | [Runbooks](54-runbooks.md) | The runbook file format, how runbooks match alerts, publishing AI-drafted runbooks, importing a folder. |
| 55 | [The metrics dashboard](55-metrics-dashboard.md) | What every number on the **Maturity** page means and what a healthy value is. |
| 56 | [The audit log](56-audit-log.md) | Reading, filtering and verifying the hash-chained log of everything the AI and your team did. |
| 57 | [Running evals](57-running-evals.md) | Benchmarking your AI SRE on scripted outages, comparing models, reading the leaderboard. |
| 58 | [Connect your agent with MCP](58-connect-your-agent-with-mcp.md) | Using Claude Code, Cursor or any MCP client against Daalu: `daalu login`, then `daalu mcp`. |

A suggested order: read 51, set a policy with 52, then work through
53–56 as your team starts grading investigations and publishing runbooks.
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
