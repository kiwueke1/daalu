# 21. Workflows and automations

The *Automations* page is where you write your own scheduled or
event-triggered behaviours. Every automation eventually becomes
an agent on the *Agents* page and runs inside Daalu's executor.

URL: `https://ops.example.com/automations`.

---

## What a workflow is

A workflow is:

- A **trigger** — what kicks it off. Either a cron schedule
  ("every Monday at 9 AM"), an event match ("when an alert with
  label `team: netops` fires"), or a manual run ("only when
  someone clicks Run").
- A **plan** — a sequence of steps. Each step is one of: a
  tool call, a conditional branch, a comment-to-Slack, a
  human-approval gate.
- An **output** — what the plan ends with. Usually one of:
  a notification, a change proposal, or just a recorded run
  log.

Workflows are tenant-scoped. You can create as many as you
want; common patterns are documented at *Help → Workflow
examples*.

---

## Authoring a workflow

Click **New automation**. The authoring UI is a sequence of
cards on a vertical canvas, each card a step.

### 1. Trigger

The first card defines when the workflow runs:

- **Cron** — pick a cron expression with a UI helper
  ("Every weekday at 09:00", "First of the month").
- **Event** — match events by source, kind, and labels.
- **Manual** — only runs when invoked from the workflow's
  page.

### 2. Steps

Each subsequent card is a step:

- **Tool call** — pick from the same catalog the Assistant
  uses: `query_prometheus`, `list_resources`, etc. The card
  has inputs for the tool's arguments.
- **Branch** — "if X, run these steps; if not, run those." X
  is a small expression over earlier-step outputs.
- **Wait for approval** — pauses the workflow until a human
  reviews and continues. Useful for sensitive plans.
- **Notify** — send a Slack/PD/email.
- **Propose** — write a structured change proposal.

Steps can reference earlier steps' outputs with a templating
syntax: `${steps.list_resources.count}`.

### 3. Save

The workflow saves with a name and description. Active
workflows show up on the Agents page.

---

## Common patterns

A few workflows we see often:

### Weekly cost briefing

- **Trigger:** Cron, Mondays 09:00.
- **Steps:** Aggregate last week's cloud spend per account →
  format as a Markdown table → post to #ops-weekly Slack.

### Cert-expiry sweep with auto-proposal

- **Trigger:** Cron, daily 02:00.
- **Steps:** List TLS certs expiring in <30 days → for each,
  open a change proposal to renew via cert-manager.

### Page when a deploy fails

- **Trigger:** Event match on `kind=deploy.failed`.
- **Steps:** Lookup the on-call → page them with PagerDuty.

### Drift sweep on a custom interval

- **Trigger:** Cron, every 15 min.
- **Steps:** For each device in site=eu-west, force reconcile
  → if any drift, open a proposal.

### Handoff briefing for the new on-call

- **Trigger:** Manual.
- **Steps:** Pull last 24 hours of alerts → pull current
  open incidents → format as a handoff note → email to the
  incoming on-call.

The community catalog at *Help → Workflow examples* has the
exact configurations for these and more.

---

## Run history

Each workflow has a run-history page:

- Last 100 runs with timestamp, status, duration.
- Drill-down per run: each step's inputs, outputs, errors.
- "Re-run with same inputs" for retrying a failed run.

Failed runs send a notification to the workflow's author by
default. You can change to a shared channel in the workflow
settings.

---

## Permissions

Workflow execution runs under Daalu's executor identity, but
the workflow's **author** is who's accountable for it. The
author is the user who created the workflow; for human-
approval steps, the approver is recorded separately.

A few rules:

- Only admins can create workflows that include write tools.
  Non-admins can author read-only workflows.
- A workflow that ends in a proposal still respects the
  four-eyes rule — the workflow's author is the proposer, so
  they can't auto-approve their own workflow's proposals.
- Workflows can be **paused** (by an admin) without being
  deleted. Paused workflows don't run; their history is
  preserved.

---

## Versioning

Workflows are versioned. Every save creates a new version.
The "Versions" tab on a workflow's detail page lets you:

- View previous versions.
- Diff between two versions.
- Roll back to an earlier version.

Useful when you tweak a workflow and it starts misbehaving —
roll back, study what changed, fix the new version.

---

## Disabling vs deleting

- **Disable** — keeps the workflow but stops the trigger from
  firing. Run history is preserved. Can be re-enabled.
- **Delete** — removes the workflow and its history. Cannot
  be undone (run logs are also lost — export them first if
  you need a record).

---

## What's next

Chapter 22 covers the *Billing* page — spend across cloud,
local GPU, and Daalu itself.
