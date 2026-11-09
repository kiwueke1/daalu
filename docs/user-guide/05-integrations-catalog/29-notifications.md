# 29. Notifications (Slack, email, PagerDuty)

The notification channels are how Daalu reaches a human. There
are two, Slack and email, and each is independent — you can have
only Slack, only email, both, or neither. PagerDuty works the
other way round: it's an alert *source*, covered at the end of
this chapter.

Both channels live in *Managed infra → Integrations →
Notification channels*. Each card shows **Configured** or
**Needs setup**.

---

## Slack

### Setup

Daalu posts to Slack through an **incoming webhook**.

1. In Slack, create an incoming webhook for your workspace (via
   a Slack app with incoming webhooks turned on) and copy its
   URL.
2. In Daalu, *Managed infra → Integrations → Notification
   channels → **Set up Slack notifications***.
3. Fill in:
   - **Incoming-webhook URL** —
     `https://hooks.slack.com/services/...`.
   - **Briefing channel** — optional, for example
     `#operations`.
   - **Incidents channel** — optional, for example
     `#incidents`. Leave it blank to use the briefing channel.
4. Click **Test connection**. Daalu posts a short test message
   through the webhook; "Slack accepted the test message" means
   it worked. Then click **Connect Slack notifications**.

To change any of this later, click **Edit Slack
notifications** on the same card.

### What Daalu posts

- **Incident updates** — status updates for an incident go to
  the incidents channel.
- **Scheduled reports** — a saved query delivered on a
  schedule (*Reports → Schedules*, Chapter 23) can go to Slack.
  Each schedule can name its own channel; if it doesn't, Daalu
  uses the briefing channel.

Daalu sends the channel name with each post. Slack may still
deliver to the channel the webhook was created for, depending on
how the webhook was set up, so create the webhook for the channel
you want most.

Daalu only sends. It doesn't read your Slack messages.

### Removing

Revoke or delete the incoming webhook in Slack (or remove the
Slack app it belongs to). Posts stop immediately.

---

## Email

### Setup

Invites and sign-up emails are sent by Daalu; you don't need to
set anything up for those.

To have Daalu send incident and report email through your own
mail relay:

1. *Managed infra → Integrations → Notification channels →
   **Set up Email (SMTP)***.
2. Fill in **SMTP host**, **Port** (usually `587`; Daalu uses
   STARTTLS), **Username**, **Password**, **From address**, and
   optionally an **On-call recipient**.
3. Click **Test connection**, then save.

### What Daalu emails

- **Incident updates** — sent to the on-call recipient, if you
  set one.
- **Scheduled reports** — a schedule in *Reports → Schedules*
  with the email destination goes to the address it names.
- **Invites** — how teammates join your organisation
  (Chapter 6).

### Your own preferences

Each person chooses what they want to hear about in *Settings →
Notifications*: **Email me on critical alerts**, **Email the
daily briefing**, and two in-app switches (a change proposal is
ready for review; a workflow failed). They're off until you turn
them on.

### Spam

Daalu emails sometimes land in junk on first send. The cause is
usually your mail filter not yet trusting the sender. Add the
from-address to your allow list on first use.

---

## PagerDuty (alert source)

PagerDuty brings your **open PagerDuty incidents into Daalu's
alerts feed**, so the AI can investigate them next to everything
else. Daalu doesn't page through PagerDuty.

1. In PagerDuty, create an API token for Daalu to read
   incidents with.
2. In Daalu, set PagerDuty up on *Managed infra → Onboarding*
   and paste the **PagerDuty API token**.
3. On *Managed infra → Integrations → Ingest sources*, the
   PagerDuty card now reads **Configured**. Click **Check now**
   to pull the current triggered and acknowledged incidents
   into the alerts feed.

Repeat firings of the same incident collapse into one alert
rather than piling up as duplicates.

---

## What's next

Chapter 30 covers Nautobot — the network source-of-truth
integration.
