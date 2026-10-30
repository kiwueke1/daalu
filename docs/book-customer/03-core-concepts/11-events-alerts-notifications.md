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

An **alert rule** is the recipe that promotes some events into
**alerts**. Rules are defined either:

- **In your observability stack.** Prometheus alerting rules and
  Grafana alert rules are pulled in by the adapter and treated
  as Daalu alert rules. You don't have to author them twice.
- **In Daalu.** *Settings → Alert rules* lets you write rules in
  Daalu's own DSL. We expect most customers to use this rarely
  — most rules already exist in their observability stack.

A rule looks roughly like: "if more than three events of kind
`device.config.drifted` arrive from source `nautobot/main` in 10
minutes, fire an alert at severity `warning` with the label
`mass-drift`."

When a rule fires, Daalu writes an **alert** row. Alerts are the
unit a human looks at. They have:

- A title, suitable for paging text.
- A severity.
- A status (`firing` or `resolved`).
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
tenant. You can filter by severity, status, source, label.

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

Whether each severity triggers a notification depends on your
**notification rules** — the rest of this chapter.

---

## Incidents: when one alert isn't enough

An **incident** is a grouping of one or more alerts under a
single ongoing investigation. Incidents have:

- A title (auto-generated or user-set).
- A status (`open` or `resolved`).
- An assignee.
- A timeline (the alerts that joined it, comments by humans,
  Assistant suggestions).
- A postmortem document (a starter is generated when the
  incident is closed).

Incidents are the right unit for cross-functional collaboration.
When an SRE pulls in a NetOps colleague to help with a routing
issue, they share an incident, not an alert.

Incidents are created two ways:

- **By an agent.** When multiple related alerts fire in a short
  window, an agent groups them and proposes opening an incident.
- **By you.** From any alert's detail page, **"Promote to
  incident."**

A single alert is *not* automatically an incident. Most alerts
don't need that overhead.

---

## Notifications: the part that pings your phone

A **notification** is the outbound message that Daalu sends to
get a human's attention. Notifications go through one or more of
your configured channels:

- **Slack** — a message in a channel or DM.
- **PagerDuty** — an incident in PagerDuty, which then handles
  paging via its own escalation policies.
- **Email** — a transactional email to the user.
- **Browser push** — a notification on the *Home* page and (if
  enabled) a desktop notification when you have the tab open.
- **SMS / phone** — only via PagerDuty integration. Daalu
  doesn't operate its own SMS gateway.

You configure which channels get used at *Settings →
Notifications*. The configuration has three levels of detail:

1. **Per-user defaults.** "By default, I want critical alerts to
   page me on PagerDuty and warnings to ping me on Slack." Each
   user sets their own.
2. **Per-route overrides.** "Alerts labelled `team: netops`
   should go to the #netops Slack channel regardless of who's on
   call." Admins set these.
3. **Per-rule overrides.** "This specific alert rule, when it
   fires, should always page the platform on-call." Set when
   authoring the rule.

The system resolves to the most specific match. If you find an
alert paged you that you didn't expect, the alert's detail page
shows the routing decision: "this alert matched
`team=platform` → routed to PagerDuty service `platform-prod`."

---

## "Should this page me?" — a cheat sheet

A quick reference. Pin this somewhere if you're new to on-call:

| Severity | What's typical | Default behaviour |
|---|---|---|
| info | Routine events | Recorded only |
| warning | Work-hours look | Slack to assignee/channel |
| critical | Wake-up | PagerDuty page |

Override any of this at *Settings → Notifications*.

---

## The Watchdog alert

One specific alert exists in every Daalu tenant: **Watchdog**. It
fires constantly. Its purpose is to verify the *Alerts pipeline
itself* — if Watchdog stops appearing in Slack/PagerDuty, you
know notifications are broken, even when no other alert has
fired in a while.

You can configure Watchdog's destination at *Settings → Alerts →
Watchdog*. We recommend pointing it at a low-volume channel
nobody routinely watches — its job is to be a heartbeat, not a
distraction. The absence of beats is the signal.

---

## Snoozing alerts

You can snooze an alert (suppress further notifications for it)
from the alert's detail page. **"Snooze for"** offers:

- 30 minutes
- 2 hours
- 8 hours ("until tomorrow")
- Until a custom time

Snoozing only affects *your* notifications. Other users on the
alert still get pinged unless they snooze too.

Snoozing doesn't resolve the alert. It silences notifications.
The alert stays *firing* until whatever's wrong gets fixed.

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
  template change) deserves *one* alert, not 200. Configure
  rate-limiting on the rule.
- **Bad labels.** If you can't route an alert, you can't tune
  it. Make sure your alert rules emit useful labels.
- **No runbook.** An alert without a documented "what to do
  when this fires" turns every page into an investigation from
  zero. Attach a runbook URL to the rule; it'll appear on the
  detail page when the alert fires.

The *Alerts* page has a **"Top alerts by volume this week"**
section at the bottom. Spend 15 minutes on that list once a week
in your first month; it pays for itself.

---

## What's next

Chapter 12 covers the AI Assistant in detail — how it works with
alerts, how the proposal mechanism works, and what the limits of
its autonomy are.
