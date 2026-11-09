# 11. Events, alerts, and notifications

Operational work is, fundamentally, the work of reacting to
signals. Daalu's job is to take the firehose of signals coming
out of your infrastructure and turn it into a small number of
things that need a human's attention. This chapter explains how
that pipeline works.

By the end of it, you'll know the difference between an event,
an alert, an incident, and a notification — and which of those
things should wake you up.

---

## Events: the raw stream

Every time an adapter observes something in your environment, it
writes an **event**.

An event has:

- A **source** — which integration produced it (e.g.,
  `aws/acme-prod`, `nautobot/main`, `prometheus/eu-cluster`).
- A **kind** — what category of thing it is (e.g.,
  `cloud.resource.created`, `device.config.drifted`,
  `metric.value.high`, `webhook.received`).
- A **severity** — `info`, `warning`, or `critical`.
- A **payload** — a JSON blob with the details (resource ID,
  metric value, drift summary, etc.).
- A **timestamp**.
- A **tenant** — implicit, set by the API on ingest.

Events are the lowest layer. You don't see them directly in the
UI most of the time — they're more granular than humans need to
read. When you do want to look at them, the *Reports → Query*
tab is the entry point: pick *events* as the entity and either
build a structured filter or just ask the Assistant in plain
English (*"events from nautobot/main in the last hour"*). See
Chapter 23 for the full Reports walkthrough.

A typical small tenant produces 1,000–10,000 events per day. A
large multi-cloud tenant produces millions. Most of them mean
nothing — they're the ambient hum of a working environment.

---

## Alert rules: turning events into alerts

An **alert rule** is the recipe that turns a signal into an
**alert**. Daalu doesn't ask you to write rules twice: they live
in the systems that already have them.

- **Prometheus / Alertmanager.** Your alerting rules stay in
  Prometheus. Every couple of minutes Daalu pulls the alerts that
  are firing (from Alertmanager, or from Prometheus itself when
  there's no Alertmanager).
- **AWS CloudWatch.** Alarms in the *ALARM* state come in every
  minute once an AWS account is connected.
- **PagerDuty.** Open PagerDuty incidents come in as alerts once
  PagerDuty is connected.
- **Your own sources.** Anything that can send an HTTP request
  can post events to Daalu's ingest endpoint (Chapter 33).

To add or change a rule, change it where it lives — for example,
in your Prometheus rule files. There's no rule editor in Daalu.

When an upstream alert fires, Daalu writes an **alert** row. Alerts are the
unit a human looks at. They have:

- A title, suitable for paging text.
- A severity.
- A status (`open`, `acknowledged`, or `resolved`).
- A start time and (if resolved) an end time.
- A set of labels (`{cluster: eu-west, device: edge-router-3}`).
- A pointer back to the events that triggered it.
- An auto-generated explanation (by an agent) once it's been
  open for ~30 seconds.

Alerts also **resolve themselves**. For alerts mirrored from an
upstream store (a Prometheus alert rule, say), Daalu tracks the
firing set each cycle: when the upstream signal stops firing,
the matching Daalu alert — and any incident it opened — is
closed automatically. Nothing lingers open after the problem
has cleared, and a re-fire of the same rule collapses back onto
one alert rather than duplicating.

The *Alerts* page in the sidebar is the inbox of alerts for your
tenant, with Open, Acknowledged and Resolved tabs.

---

## Severity — what each level means

Daalu's three severities map to the same intuition most
on-call systems use:

- **info** — Worth recording. Not worth paging. Examples: a new
  EC2 instance came up; a routine deploy succeeded; a daily
  cron finished.
- **warning** — Worth a human look during work hours. Examples:
  one node in a pool is unhealthy; a metric crossed a soft
  threshold; a non-critical service is degraded.
- **critical** — Worth waking someone up. Examples: customer-
  facing service is down; a security policy was modified;
  the operator app's own health is degraded.

Whether an alert reaches you outside Daalu depends on the
channels you've connected and your own notification settings —
the rest of this chapter.

---

## Incidents: when one alert isn't enough

An **incident** is a record that something bigger than one alert
is going on. Incidents have:

- A title and an optional summary (scope, who's impacted).
- A severity.
- A status (`open` or `resolved`).
- The AI's likely root cause and suggested remediation, when the
  AI opened it.
- Evidence — the alerts and events behind it.

Incidents are the right unit for cross-functional collaboration.
When an SRE pulls in a NetOps colleague to help with a routing
issue, they share an incident, not an alert.

Incidents are created two ways:

- **By the AI.** When it triages a serious alert, the AI can open
  an incident with its findings attached.
- **By you.** From any alert's detail page, **"Promote to
  incident."**

Open incidents are counted on the home page's **Open now** tile
and appear in briefings; you can list them on *Reports → Query*.
To announce one to your team, run the **Coordinate an incident**
automation (Chapter 21), which posts to your incidents Slack
channel.

A single alert is *not* automatically an incident. Most alerts
don't need that overhead.

---

## Notifications: the part that pings your phone

A **notification** is the outbound message that Daalu sends to
get a human's attention. Two channels are available, set up under
*Managed infra → Integrations → Notification channels*:

- **Slack** — where Daalu posts daily briefings and incident
  updates.
- **Email (SMTP)** — on-call escalations, optional daily-briefing
  emails, and invites.

Paging belongs to the tool you already page with: Daalu brings
PagerDuty incidents *in* as alerts, and doesn't send pages or
SMS of its own.

Each person chooses what reaches them in *Settings →
Notifications*:

- Email me on critical alerts.
- Email the daily briefing.
- In-app: a change proposal is ready for review.
- In-app: workflow failed.

All four start off; turn on what you want. Inside the app, the
right rail's **Alerts** section and the sidebar badges show what's
firing and what's waiting for you, whatever your settings.

Saved reports can also be delivered on a schedule to Slack or
email from *Reports → Schedules* (Chapter 23).

---

## "Should this page me?" — a cheat sheet

A quick reference for what each severity usually deserves. Your
paging tool decides who gets woken up; this is how to think about
it:

| Severity | What's typical | Sensible handling |
|---|---|---|
| info | Routine events | Recorded only |
| warning | Work-hours look | Slack, or the Alerts page |
| critical | Wake-up | Page through your paging tool; email if you've turned it on |

---

## Alert noise and runbook tuning

The most common feedback on alerts after a month of use is "too
many of them." A few patterns:

- **Flapping metrics.** A metric that bounces above and below a
  threshold generates an alert every time. Add a "for" duration
  to the rule — "value > 80 for 5 minutes" suppresses brief
  spikes.
- **Mass-event noise.** A storm of events from one source
  (e.g., 200 device drifts at once because someone applied a
  template change) deserves *one* alert, not 200. Daalu already
  folds re-fires of the same alert onto one card ("fired N×");
  for the rest, group or rate-limit in the rule upstream.
- **Bad labels.** If you can't route an alert, you can't tune
  it. Make sure your alert rules emit useful labels.
- **No runbook.** An alert without a documented "what to do
  when this fires" turns every page into an investigation from
  zero. Write the fix down as a runbook on the **Runbooks** page
  (Chapter 54); a published runbook that matches an alert goes
  into the AI's triage context.

To find your noisiest alerts, sort by the **Fired N×** count on
the Alerts page, or ask the Assistant ("which alerts fired most
this week?"). Spend 15 minutes on that list once a week in your
first month; it pays for itself.

---

## What's next

Chapter 12 covers the AI Assistant in detail — how it works with
alerts, how the proposal mechanism works, and what the limits of
its autonomy are.
