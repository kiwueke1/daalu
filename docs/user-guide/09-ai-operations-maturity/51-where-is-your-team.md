# 51. Where is your team?

The **Maturity** page answers one question: how well does your team work
with the AI SRE today? It doesn't ask you to fill in a survey to find out.
It measures what happened over a window (the last 7, 30 or 90 days), reports
the metrics, and uses thresholds on those metrics to place you on the AI-Ops
Maturity Model ladder from Level 0 to Level 5.

URL: `https://daalu.example.com/maturity` (sidebar: **Insights → Maturity**).

---

## Start with the metrics

Five TRACE metrics say how your AI operations are performing, in the same way
the DORA metrics do for software delivery:

| Group | Metric | Tile on the page | Better is |
|---|---|---|---|
| Speed | Time to triage | **Time to triage** | Lower |
| Speed | Time to resolve | **MTTR** | Lower |
| Quality | Agreement rate | **Agreement rate** | Higher |
| Quality | Change-failure rate | **Change-failure rate** | Lower |
| Autonomy | Escalation rate | **Escalation rate** | Lower |

Supporting metrics explain them: **AI triage rate** and the alert count (how
much the AI sees), **verdict coverage** (how much of its work you grade, the
sample behind agreement rate), **approval rate** (how often you accept its
fixes), published **runbooks**, **eval runs**, and whether the **audit
chain** verifies. [Chapter 55](55-metrics-dashboard.md) explains every one.

---

## How the metrics map to the levels

### The six levels

| Level | Name | In one sentence |
|---|---|---|
| 0 | Ad hoc | Individuals paste logs into a chatbot. No shared context, no record. |
| 1 | Assisted | AI reads alerts, logs, metrics and explains them. Read-only. |
| 2 | Proposing | AI names the root cause and proposes a fix; every change is human-approved. |
| 3 | Trusted | Policy lets low-risk, proven actions run automatically. |
| 4 | Learning | Every incident improves the next: verdicts, runbooks, evals, metrics. |
| 5 | Autonomous | AI resolves most routine incidents; humans own policy and exceptions. |

Those are the exact descriptions the page shows when you hover a level.

### The thresholds behind each level

Each level from 1 to 5 is a short list of criteria. Most are thresholds on
the metrics above; a few are facts about what ran and what your saved policy
allows. Daalu checks every one against the data in the window. Your
**automatic level** is the highest level whose criteria are all met **and**
every level below it is met too. Levels are cumulative: if you meet Level
4's criteria but not Level 3's, you are at Level 2.

| Level | Criterion (as shown on the page) | Metric or fact behind it | Met when |
|---|---|---|---|
| 1 | Alerts investigated by the AI | AI triage rate | at least 50% of the alerts in the window have an AI investigation |
| 1 | Alerts in the window | Alert count | at least 5 alerts reached Daalu in the window |
| 2 | Human-approved fixes executed | Fact: what ran | at least 1 fix a person approved has run |
| 2 | Proposal approval rate measured | Approval rate | at least one proposal has been approved or rejected, so the rate is known |
| 3 | Policy rules allowing automatic actions | Fact: saved policy | your **saved** policy has at least one `auto` or `auto_after_trust` rule (a `defaults.mode` of `auto` or `auto_after_trust` also counts as one) |
| 3 | Automatic actions executed | Fact: what ran | at least 1 automatic action has run |
| 3 | Change-failure rate | **Change-failure rate** (key) | 15% or less of the executed fixes failed verification |
| 4 | Investigations with a human verdict | Verdict coverage | at least 30% of investigations have a verdict |
| 4 | Published runbooks | Runbooks published | at least 1 published runbook |
| 4 | Eval runs in the window | Eval runs | at least 1 eval run in the window |
| 5 | Resolved automatically | **Escalation rate** (key) | at most 50% of resolved investigations needed a human |
| 5 | Agreement rate | **Agreement rate** (key) | at least 80% agreement between the AI's root cause and your verdicts |
| 5 | Audit chain verifies | Audit chain | the audit log's hash chain is intact |
| 5 | Guardrails configured (circuit breaker + rate limit) | Fact: saved policy | your **saved** policy sets both `guardrails.circuit_breaker` and a non-zero `guardrails.max_auto_actions_per_hour` |

Read from the metric side: of the five TRACE metrics, change-failure rate
gates Level 3, and escalation rate and agreement rate gate Level 5. Time
to triage and time to resolve gate no level, on purpose. Every team's
systems and alert mix differ, so there is no fair single threshold; they are
what climbing the levels should improve. Watch them against your own
history, for example by comparing the 30-day and 90-day windows.

Two details worth knowing:

- **Policy criteria only look at a saved policy.** The built-in default
  (what's in force before anyone saves a policy) has no automatic rules, and
  its guardrails don't count toward Level 5 until you save a policy of your
  own.
- **A rate with no data is "unknown", not zero.** If no fixes have run, the
  change-failure rate is unknown and that criterion is "not yet".

Every metric behind these criteria is explained in
[Chapter 55](55-metrics-dashboard.md).

---

## Reading the Maturity page

Top right: the **window** switch — **7 days**, **30 days**, **90 days**.
Everything on the page recomputes for the window you pick.

### The headline

A large **L***n* with the level's name, its description, and the line "The
highest level whose criteria (and every level below) are all met." If
someone has saved a self-assessment, a **Self-assessed** box sits on the
right with that level, who saved it, and when.

### The ladder

Six tiles, Level 0 to Level 5. Your current level is highlighted. Each tile
shows whether that level's *own* criteria are met. You can meet a higher
level's criteria without being at it yet — the ladder makes that visible, so
you can see which lower-level gap is holding you back.

### The stat tiles

Eight tiles with the headline numbers. Five of them are the TRACE metrics
(time to triage, MTTR, agreement rate, change-failure rate and escalation
rate); the other three are supporting metrics:

| Tile | What it shows |
|---|---|
| **AI triage rate** | Share of alerts the AI investigated, with "*n* of *m* alerts" underneath |
| **Time to triage** | Median time from the alert firing to the AI's root cause |
| **Agreement rate** | How often your team agreed with the AI's root cause, with verdict coverage underneath |
| **Approval rate** | Share of human-gated proposals that were approved, with approved / rejected counts |
| **Change-failure rate** | Share of executed fixes that failed verification; turns red above 15% |
| **Escalation rate** | Share of resolved investigations that needed a human (not fixed automatically) |
| **MTTR** | Median time from the alert firing to resolution |
| **Audit chain** | "Intact" or "Broken", with a link to the audit log |

A dash (—) means there isn't enough data to compute the number yet.

### Daily trend

A line chart over the window: **Alerts**, **Investigated**,
**Auto-resolved** and **Resolved by approved fix** per day.

### Criteria

The threshold table from the section above, filled in with your numbers. Columns:
**Level**, **Criterion**, **Met** ("met" or "not yet"), **Value** and
**Target**.

### Next steps

Up to six concrete suggestions: the unmet criteria of the next level first,
then the level after. Each tells you which page to go to — for example
"Autonomy policy → set `investigate_automatically: true` so every new alert is
investigated." At Level 5 it reminds you to keep verdicts and evals running
so the level holds.

---

## The self-assessment

Some practices don't show up in data: does on-call actually read the AI's
root cause before acting? Is the policy reviewed? The **Self-assessment**
section at the bottom of the page asks 16 yes/no questions. Click **Yes** or
**No** for each, then **Save self-assessment**.

Your self-assessed level uses the same rule as the automatic one: the highest
level whose questions, and every lower level's, are all answered yes. An
unanswered question counts as no.

The self-assessed level is shown **beside** the automatic level, never
instead of it. The headline is always the measured one.

**Level 1 — Assisted**

- Production alerts reach Daalu (or your AI tool) automatically, not by copy-paste.
- The AI has read access to the same logs, metrics and cluster state on-call uses.
- Investigations happen in a shared, recorded place instead of personal chatbot sessions.

**Level 2 — Proposing**

- On-call reviews the AI's root cause before acting on it.
- Fixes the AI proposes are applied through the approval flow, not by hand on the side.
- When a proposal is rejected, the reason is written down.

**Level 3 — Trusted**

- The autonomy policy is changed through code review and reviewed at least quarterly.
- The team has agreed which actions are low-risk enough to run automatically, per environment.
- Everyone on call knows how to use the kill switch, and it has been tested.

**Level 4 — Learning**

- Post-incident reviews include the AI's investigation and a verdict on it.
- Runbooks are kept as code and updated after incidents (including AI-drafted ones).
- Model or prompt changes are evaluated against the scenario suite before rollout.
- AI-Ops metrics (agreement, change failure, escalation rate) are reviewed on a regular cadence.

**Level 5 — Autonomous**

- Humans are paged only for exceptions the policy can't handle.
- The audit log is verified and sampled regularly (security/compliance owns it).
- There's an explicit error budget for automatic changes; breaching it demotes autonomy.

Anyone on your team can save a self-assessment. The page shows the most
recent one.

---

## A typical climb

Each step moves a metric, and the metric moves the level.

- **Level 0 → 1: raise the triage rate.** Connect your alert source ([chapter 8](../02-getting-started/08-onboarding-your-observability-stack.md)) and turn on
  `investigate_automatically` in the autonomy policy (chapter 52).
- **Level 1 → 2: get the approval rate measured.** Approve or reject the
  fixes the AI proposes on the alert page, so a human-approved change runs
  and is verified.
- **Level 2 → 3: start automatic fixes, keep the change-failure rate low.**
  Add an `auto_after_trust` rule for a low-risk action you approve often. It
  runs on its own once it has a clean record.
- **Level 3 → 4: raise verdict coverage.** Grade investigations, publish a
  runbook, run an eval.
- **Level 4 → 5: lower escalation rate, keep agreement rate high.** Widen the
  automatic rules to your most frequent proven fixes and save a policy with
  a circuit breaker and an hourly cap.

Along the way, time to triage and time to resolve should fall. If they
don't, look at where alerts wait before anyone (or the AI) picks them up.

The team-facing version of this plan, as a 90-day schedule, is in
[docs/maturity-model.md](../../maturity-model.md).

---

## What's next

Chapter 52 walks through the **Autonomy policy** page: how to write the
policy that moves you from Level 2 to Level 3 and beyond.
