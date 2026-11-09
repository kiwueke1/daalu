# 21. Workflows and automations

This chapter covers two related pages: *Automations*, the jobs you
can start by hand, and *Workflows*, the remediations the AI has
run for your alerts.

URLs: `https://ops.example.com/automations` and
`https://ops.example.com/workflows`.

---

## Two pages: Automations and Workflows

The sidebar has two entries for this, in different groups:

- **Automations** (under *Automate*) — automations you can start
  by hand, each with a plain-English line on what it does, and
  **Recent runs** below. Click an automation's run button (for
  example **Open incident**) and it asks you to confirm in place,
  saying what will happen — "Starts it right away", or "Posts to
  your incidents channel" — before anything runs. A short message
  confirms it started, and its run appears under Recent runs.
- **Workflows** (under *Respond*) — the remediations the AI has
  run for alerts. Each row links to a step-by-step detail page and
  to the alert it remediated. When a remediation plan is waiting
  for your approval, a banner at the top says so, with a **Review
  plan** button that opens that alert's *Remediation workflow*
  tab. The sidebar shows the same count on **Workflows** ("1 to
  approve") from any page.

---

## What a workflow is

A workflow is a plan carried out step by step — each step a tool
call, with its result recorded. Two things produce them:

- **Automations** that ship with your modules. Today that's
  **Coordinate an incident**: it opens an incident, marks it as
  investigating, drafts a status update and posts it to your
  incidents Slack channel (and email, if set). You give it an
  **Incident title** and confirm.
- **AI remediations.** When you approve the fix the AI proposed
  for an alert (on the alert's *Remediation workflow* tab), Daalu
  runs it as a workflow — or runs it on its own, where your
  autonomy policy allows.

There's no editor for writing your own automations in the app.
For something you want done on a schedule, two features cover
most needs:

- **Scheduled reports** — save a query on *Reports → Query* and
  deliver it to Slack or email on a schedule from *Reports →
  Schedules* (Chapter 23). A weekly cost or alert summary is a
  good fit.
- **Your own scripts against the API** — with a personal access
  token (Chapter 37), anything you can script can read from and
  write to Daalu, and post events to it (Chapter 33).

If you need an automation Daalu doesn't ship, tell us at
your operator or through *Help & Feedback*.

---

## Run history

- **Automations → Recent runs** lists every run of a hand-started
  automation, newest first, with its status.
- **Workflows** lists every remediation the AI has run, with its
  status and the alert it came from. Click **Open** on a row for
  the run's page: each step in order with what it did and its
  outcome, any error, and a link back to the alert.

---

## Permissions and safety

- Remediations follow your autonomy policy (Chapter 52): a fix is
  denied, waits for approval, or runs on its own according to the
  rule that matches. A rule can require more than one approver,
  and the same person can't approve twice.
- Every step a workflow runs is recorded in the audit log
  (*Insights → Audit log*), including whether a person approved it
  or the policy ran it.
- A run can't be stopped once it's started; it runs to completion
  or fails. That's why fixes are approved before they run.

---

## What's next

Chapter 22 covers the *Usage & Pricing* page — your plan, AI
usage, and your local GPU.
