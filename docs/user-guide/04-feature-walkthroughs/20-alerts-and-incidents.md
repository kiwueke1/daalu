# 20. Alerts and incident response

The *Alerts* page is the operational inbox. Chapter 11 covered
the model — events, alerts, incidents, notifications. This
chapter is the UI walkthrough.

URL: `https://ops.example.com/alerts`.

---

## The list view

The default Alerts page shows currently-firing alerts at the
top, then recently-resolved ones below. Columns:

- **Severity badge** — info / warning / critical.
- **Title** — the alert's headline (from the rule).
- **Source** — the integration that produced it.
- **Labels** — pills showing the alert's labels (cluster,
  device, service, team).
- **First seen** — when the alert was first noticed.
- **Assignee** — who's looking at it. Empty means nobody.
- **Status** — firing / resolved / snoozed.

Top of page filters: severity (multi-select), source, status,
assignee, label search, full-text search.

Top-right actions: **Mute all warnings** (snooze every warning
for 1 hour, useful during maintenance windows) and **Acknowledge
all** (set yourself as the assignee on every unassigned
critical).

---

## Alert detail page

Click an alert to drill in. The detail page has:

### Header

- Title, severity, status pill.
- Buttons: **Acknowledge** (assign to me), **Snooze**,
  **Resolve manually**, **Promote to incident**.

### Auto-generated explanation

A paragraph or two written by the alert-explainer agent (see
Chapter 19). Inputs cited at the bottom — click any to drill in.

If the explanation looks wrong, click "Send feedback." Real
human eyes on the engineering side review these and the agent's
prompts get tuned over time.

### Suggested actions

A list of buttons the agent recommends:

- "Restart deployment X"
- "Page on-call Y"
- "Open change proposal Z"
- "Snooze for 1 hour while we wait for the planned maintenance"

Clicking any of these is the trigger; mutations still go
through a proposal.

### Timeline

The history of this alert:

- Fired at T0.
- Acknowledged by Jane at T+2m.
- Comment by Bob at T+5m.
- Resolved at T+12m.

Includes any Assistant interactions on this alert.

### Comments

A threaded discussion. Each comment notifies subscribers.

### Labels

The alert's labels in a table, editable inline. Editing labels
on a single alert is occasionally useful for routing
("oh, this should have been tagged `team: netops`"). Editing the
*rule* that produces the alert is usually what you want; that's
in *Settings → Alert rules*.

### Related

A panel showing other recent alerts with overlapping labels.
Click any to jump.

---

## The AI investigation and approve cards

When the AI investigates an alert, the alert page shows its tabs —
**Root cause**, **Background**, **Remediation workflow**, **Evidence** —
and a **Re-triage** button to run a fresh pass.

**Investigation record.** Under the **Root cause** tab, a panel shows the
learning-loop record for this alert: a resolution chip ("Open",
"Auto-resolved", "Resolved (approved fix)", "Resolved manually",
"Unresolved"), **Fix verified** or **Fix did not verify**, time to triage
and time to resolve, the counts of fixes proposed / approved / rejected /
auto, and how many runbooks the AI used. Once the AI has named a root cause
you can grade it here — **Correct**, **Partly right** or **Wrong**, with an
optional note. **All investigations** opens the same record on the
Investigations page. See
[Chapter 53](../09-ai-operations-maturity/53-investigations-and-verdicts.md).

**Approve cards.** Every fix the AI proposes (on the **Remediation workflow**
tab and in the alert chat) now shows what your autonomy policy decided:

- A policy chip with the mode, and either "rule *name*" or "policy defaults".
- "Why:" — the policy's reason in plain English.
- "*n* / *N* approvals" when a fix needs more than one approver. While more
  than one approval is still needed the button reads **Add my approval**;
  after you click it you see "Your approval is recorded, waiting for *N*
  more." The fix runs only when enough different people have approved.
- A blue **Preview:** line saying what the fix will do ("Roll back
  deployment … from revision 7 to 6: image … → …. All 2 pod(s) are
  replaced.") and its blast radius in pods and deployments.
- **Auto-executed by policy** *rule* when the policy let the fix run on its
  own — no approval was needed.
- An **mcp:***email* chip when an external agent proposed the fix over MCP.
- After the fix runs: **Verified: workload recovered** or **Verification
  failed**, from the check Daalu runs once the change has settled.

Fixes the policy denies never get a card: the AI is told to propose
something else. How to write the policy is
[Chapter 52](../09-ai-operations-maturity/52-setting-your-autonomy-policy.md).

---

## Promote to incident

Some alerts deserve a full incident — multiple alerts firing
together, customer impact, on-call coordination needed.

Click **Promote to incident** on an alert to create the
incident. The dialog asks for:

- Title.
- Severity (defaults to the alert's).
- Assignee (defaults to you).
- Additional alerts to attach.

The incident gets its own page (URL
`/incidents/<id>`) with a timeline that all attached alerts
contribute to.

---

## Incident page

The incident page extends the alert detail page with:

- **Timeline** — every event (alerts joining, comments,
  Assistant suggestions, manual actions) chronologically.
- **Attached alerts** — currently in scope of the incident.
- **Postmortem draft** — auto-generated outline when the
  incident is resolved. You edit it and "Save as final" to
  preserve.

Incidents are notably less common than alerts. Most teams open
1–3 per month. They're the unit you bring to a retrospective.

---

## Snoozing

From any alert detail page:

**Snooze for** → 30 min / 2 hours / 8 hours / Custom.

What snoozing does:

- Suppresses notifications for that alert for the duration.
- Keeps the alert status `firing` (snoozing isn't resolving).
- Logs the snooze in the timeline with the snoozer's name.
- Only affects *your* notifications, not your teammates'.

What snoozing doesn't do:

- Resolve the alert. The underlying condition is still there.
- Affect downstream PagerDuty escalation paths. (Snooze in
  Daalu is independent of PagerDuty's mute, by design — you
  almost never want to silence both at once.)

A snoozed alert shows a "Snoozed by X until Y" pill at the top
of its detail page.

---

## Triggering a test alert

For verifying the alerting pipeline:

*Settings → Alerts → Send test alert.*

The dialog lets you pick severity, label set, and target
channels. Daalu fires the alert end-to-end through the same
pipeline a real one would use, then auto-resolves it after 60
seconds.

Useful when:

- You just configured Slack / PagerDuty and want to confirm
  the routing works.
- You're testing a new alert rule's labels.
- You're onboarding a new on-call and want them to see what a
  real page looks like.

---

## Alert rules

Hover over the alert title — you'll see the rule that produced
it. Click for the rule detail page:

- **Conditions** — the predicate that matches events.
- **Severity** — the rule's severity (overridable per fire).
- **Labels** — labels emitted on each fire.
- **Runbook URL** — link shown on every firing instance.
- **Recent fires** — history of this rule firing.

Editing rules is in *Settings → Alert rules* and is admin-only.

---

## Bulk operations on alerts

For when you have a storm:

- Multi-select with checkboxes.
- **Acknowledge selected** — assign yourself to all selected.
- **Snooze selected for 2 hours** — bulk snooze.
- **Resolve selected** — only available if you're allowed to
  resolve. Use rarely; alerts are supposed to auto-resolve
  when the condition clears.
- **Promote selected to incident** — bundle into one incident.

---

## What's next

Chapter 21 covers *Workflows / Automations* — the page where
you author your own agents and scheduled jobs.
