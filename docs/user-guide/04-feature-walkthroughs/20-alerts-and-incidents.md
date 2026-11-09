# 20. Alerts and incident response

The *Alerts* page is the operational inbox. Chapter 11 covered
the model — events, alerts, incidents, notifications. This
chapter is the UI walkthrough.

URL: `https://ops.example.com/alerts`.

---

## The list view

The Alerts page has three tabs — **Open**, **Acknowledged** and
**Resolved** — with counts on the first two. Within a tab, alerts
are sorted most severe first, then most recently seen.

Each alert is a card with:

- **Severity** — info / warning / critical, also shown as the
  coloured stripe down the card's edge.
- **Title** — the alert's headline (from the rule).
- **Source** — the module or integration that produced it.
- **Fired N×** — when the same alert has fired more than once.
- **Last seen** — how long ago it last fired.
- **Acknowledge** (open alerts) — moves it to the Acknowledged
  tab, so your team knows someone is on it. It stays open.
- **Resolve** — asks you to confirm in place (**Resolve alert**)
  and says what happens: the alert closes and its investigation
  stops.

An empty tab says why it's empty rather than showing a blank
list — for example, *"Nothing is firing. Alerts appear here when a
connected source reports a problem."*

---

## Alert detail page

Click an alert to drill in. The detail page has:

### Header

- Title, severity, status, and when it was last seen.
- Buttons: **Acknowledge**, **Promote to incident**, and
  **Resolve** (which confirms in place, as on the list).
- **Review plan** — appears when the AI has fixes waiting for
  approval, with a count ("1 to approve"). It takes you to the
  fix.

### Tabs

Below the header, the investigation fills the page as tabs:
**Root cause**, **Background**, **Remediation workflow**,
**Evidence**, and **Details**. The Details tab holds the alert's
occurrences, its original body, and raw metadata. The next
section describes the investigation tabs.

### The remediation chat

On the right is a chat about this alert, in place of the usual
right rail. It starts collapsed as a thin column labelled
*Remediation copilot*; click it to open the chat. It's where the
AI explains what it found and where approve cards for its fixes
appear.

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
incident. A dialog opens; it explains that the alert stays open
and is linked from the incident's evidence, and asks for:

- **Title** (defaults to the alert's).
- **Severity** (defaults to the alert's).
- **Summary** (optional) — the scope, and who's impacted.

Click **Open incident**. The alert's header then shows *Promoted to
incident …* with the incident's severity.

Incidents don't have a page of their own. You'll find them:

- In the **Open now** tile on Home, which counts open incidents
  alongside open alerts.
- In the daily briefing's *Active incidents* section.
- On *Reports → Query*, where *incidents* is one of the things
  you can list and filter.

To tell your team, run the **Coordinate an incident** automation
on the *Automations* page (Chapter 21). It opens an incident,
marks it as investigating, drafts a status update and posts it to
your incidents Slack channel (and email, if set).

---

## Quieting an alert

There's no snooze in Daalu. The options are:

- **Acknowledge** it, so your team can see someone is on it. It
  moves to the Acknowledged tab and stays open.
- **Resolve** it, if the problem is gone. Alerts mirrored from
  Prometheus or CloudWatch also resolve themselves when the
  upstream alert stops firing.
- **Silence it at the source** — for example an Alertmanager
  silence during a maintenance window. Daalu only brings in what's
  firing, so a silenced alert stays out.

---

## Sending a test alert

To check the pipeline end to end — say, after connecting Slack —
post a test event to Daalu's ingest endpoint with your
organisation's ingest key. Chapter 33 has the request to copy.
For a quick look at what alerts feel like in Daalu, the **Demo
sources** on *Managed infra → Integrations* feed sample alerts
instead.

---

## Alert rules

Daalu doesn't have its own rule editor. The rules that produce
alerts live where they already are — in Prometheus, in your AWS
CloudWatch alarms, in PagerDuty — and you change them there.
Chapter 11 explains how each source flows in.

---

## Handling a storm

There's no multi-select on the Alerts page. When many alerts fire
at once:

- Alerts that re-fire collapse onto one card ("fired N×"), so a
  flapping rule doesn't bury the list.
- The list is sorted most severe first, so start at the top.
- Promote the main one to an incident and work from there.
- Silence noisy rules at the source while you deal with the cause.

---

## What's next

Chapter 21 covers *Automations* and *Workflows* — the jobs you
can start by hand, and the remediations the AI has run.
