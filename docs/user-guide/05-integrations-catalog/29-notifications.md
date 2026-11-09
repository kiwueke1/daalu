# 29. Notifications (Slack, PagerDuty, email)

The notification integrations are how Daalu reaches a human.
Each is independent — you can have only Slack, only PagerDuty,
both, neither.

---

## Slack

### Setup

1. *Managed Infra → Notifications → Add → Slack.*
2. Click **"Authorize with Slack."** A new tab opens to
   Slack's OAuth flow.
3. Pick the workspace, accept the requested scopes
   (`chat:write`, `channels:read`, `users:read`).
4. You're redirected back to Daalu with the connection
   active.
5. In the next dialog, pick which channels Daalu may post
   to. Daalu has to be invited to private channels with
   `/invite @daalu` from inside Slack.

### Capabilities

- Post to channels you authorized.
- Post to user DMs (for personal notifications).
- React with emoji on its own messages (for
  acknowledgment indicators).
- Read recent messages in a channel (only on demand, via
  the Assistant; not stored).

Daalu **cannot:**

- Post in channels you didn't authorize.
- Read messages outside its allowed channels.
- Mention users it can't see.

### Per-channel routing

In *Settings → Notifications*, set which Slack channels get
which kinds of alert. Common pattern:

- `#ops-alerts` — all warnings and criticals.
- `#netops` — alerts labelled `team: netops`.
- `#proposals` — change-proposal notifications.
- `#daalu-heartbeat` — the Watchdog alert.

### Removing

*Managed Infra → Notifications → Slack row → Disconnect.*
Posts stop immediately. In Slack, also remove the Daalu app
from the workspace via `Apps → Daalu → Remove app`.

---

## PagerDuty

### Setup

1. In PagerDuty, create a service (or pick an existing one)
   you want Daalu's alerts to flow into.
2. Add an **Events API V2 integration** to that service.
   Copy the integration key.
3. *Managed Infra → Notifications → Add → PagerDuty* in
   Daalu. Paste the integration key.
4. Daalu fires a test event; you should see "Triggered,
   then resolved by Daalu" in the service.

### Capabilities

- Trigger PagerDuty incidents (severity → priority mapping
  configurable).
- Resolve PagerDuty incidents when the underlying alert
  clears.
- Add a note when a Daalu user acknowledges.

PagerDuty's escalation policies are what actually do the
paging. Daalu is just the source.

### Multiple services

Common pattern is one PagerDuty service per on-call rotation:

- `platform-prod`
- `netops`
- `data-platform`

Connect each as a separate Daalu integration. Route alerts to
the right one based on labels.

### Removing

Disconnect in Daalu; optionally delete the Events API V2
integration in PagerDuty.

---

## Email

### Setup

Daalu's transactional email is configured at the platform level —
the from-address and SMTP relay are managed by us. You don't
configure anything tenant-side except:

1. Per-user "send me email notifications" toggle.
2. Per-route "send to this email" rule in *Settings →
   Notifications.*

### Capabilities

- Alert digests (configurable cadence).
- Daily briefing as an HTML email.
- Mentions in change proposals.
- Invite emails (this is how users get added to your tenant).

### Limits

- 100 emails per tenant per hour by default. Mostly enough.
- Increase by writing to support if you have a use case (a
  big weekly digest distributed across the team).

### Spam

Daalu emails sometimes land in junk on first send. We've
authenticated SPF/DKIM/DMARC properly; the cause is usually
your mail filter not having a positive sender reputation yet.
Allow-list your deployment's notification sender address on first use.

---

## Browser push

Not a separate integration — built into the operator app. Enable
in *Settings → Preferences → Browser push.* Daalu asks for
permission; once granted, you get desktop notifications when
the tab is open.

---

## Suppression and rate-limiting

Two protections built into Daalu's notification system:

- **Deduplication.** The same alert firing twice in a short
  window only generates one notification.
- **Per-channel rate limit.** Daalu won't flood a Slack
  channel with more than a configurable rate of messages per
  minute. Excess gets batched into a digest.

Defaults are sensible. Tune in *Settings → Notifications →
Advanced.*

---

## What's next

Chapter 30 covers Nautobot — the network source-of-truth
integration.
