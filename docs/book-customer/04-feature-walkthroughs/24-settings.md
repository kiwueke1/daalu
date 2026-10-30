# 24. Settings — profile, preferences, API tokens

The *Settings* page is the catch-all for personal and tenant-
level configuration. It's split into several sub-pages along a
secondary nav inside the page.

URL: `https://ops.example.com/settings`.

---

## Layout

A vertical nav on the left, with sections:

- **Profile** — your own account.
- **Preferences** — UI prefs (theme, density, notifications).
- **API tokens** — your personal access tokens.
- **Team** *(admin)* — other users in the tenant.
- **Tenant** *(admin)* — tenant-wide settings.
- **Integrations** *(admin)* — alias of *Managed infra →
  Integrations* for convenience.
- **Alert rules** *(admin)* — author and edit alert rules.
- **Notifications** *(admin)* — global notification routing.
- **Briefings** *(admin)* — briefing time, anomaly thresholds.
- **Reconciliation** *(admin)* — SoT reconciler defaults.
- **LLM router** *(admin)* — local vs cloud overrides.
- **Audit log** *(admin)* — every state-changing action.

Items marked *(admin)* are hidden from non-admin users.

---

## Profile

Edit:

- **Full name.**
- **Email.** Changing your email triggers a confirmation flow.
- **Password.** Current password required to change.
- **Avatar.** Uploaded image or use email-Gravatar.
- **Time zone.** Drives briefing times, schedule UIs, etc.

A "Sign out everywhere" button invalidates every session you
have, including this one.

---

## Preferences

Per-user UI prefs:

- **Theme** — light, dark, system-follow.
- **Density** — compact vs comfortable. Affects table row
  heights.
- **Accent color** — for the highlighted UI elements.
- **Sidebar collapsed by default** — yes/no.
- **Default page on login** — Home / Alerts / Operations /
  Last-visited.
- **Per-channel notification toggles** — fine-grained on/off
  for Slack/email/push on each kind of notification.

Saved automatically as you change them.

---

## API tokens (PATs)

Personal access tokens are the durable credential for scripts
and CI. Chapter 37 covers how to use them; this page is the
management UI.

### Token list

A table with:

- Name (you chose).
- Prefix (first 12 chars of the token).
- Created.
- Last used.
- Expires (if set).
- Status (active / revoked / expired).
- **⋯** — Rotate, Revoke, Delete.

### Creating a token

**New token** opens a dialog:

- **Name** — for your future reference.
- **Expires** — never (default), 30 days, 90 days, 1 year,
  custom.
- **Optional description.**

Click **Create.** The cleartext token is shown **exactly once**.
Copy it now; you cannot retrieve it later. Paste it into the
secret store of whatever's going to use it.

### Token format

Tokens look like `dpat_xxxxxxxxxxxxxxxxxxxxxxxx`. The `dpat_`
prefix makes them easy to spot in env files and git diffs (and
makes secret scanners catch them if you commit by accident).

### Revoking

Click **⋯ → Revoke.** Immediate; any in-flight request using
the token gets a 401 next call.

A revoked token can be reactivated within 7 days. After that
it's gone for good.

---

## Team (admin only)

The same as Chapter 6 — list of users, invite, role change,
remove.

---

## Tenant (admin only)

Tenant-wide settings:

- **Tenant name.**
- **Time zone** (tenant default; overridden per user).
- **Default briefing time.**
- **Data retention windows** — how long events/alerts/proposals
  stay in the database. Defaults depend on plan.
- **Export tenant data** — generate a full JSON export.
- **Delete tenant** — irreversible.

---

## Alert rules (admin only)

The list and editor of custom alert rules (rules defined in
Daalu, as opposed to those mirrored from your Prometheus). The
editor has the same shape as the workflow trigger card —
predicate, severity, labels, runbook URL.

---

## Notifications (admin only)

The routing layer described in Chapter 11:

- **Default route** — what kind of channel each severity goes
  to by default.
- **Per-team routes** — by label match, override the default.
- **Per-rule routes** — set on the alert rule directly.
- **Watchdog config** — Watchdog alert's destination channel.

---

## Briefings (admin only)

Tenant-wide briefing configuration:

- **Default time.**
- **What sources feed the briefing** (toggles per source).
- **Anomaly thresholds** — for the cost-anomaly agent.

---

## Reconciliation (admin only)

For the SoT reconciler:

- **Default interval** (5 min).
- **Per-device overrides** (table).
- **Concurrency limit.**
- **Read-only mode** — proposals are written but never executed.

---

## LLM router (admin only)

Per-tier overrides for the LLM router:

- **Classifier** — always local / always cloud / prefer local
  (default).
- **Chat** — same.
- **Embeddings** — same.

Plus a "force fallback to cloud" big red button for emergency
debugging.

---

## Audit log (admin only)

Every state-changing action in your tenant:

- Who did it (user, agent, executor).
- What was changed.
- When.
- Result.

Filterable and exportable. The compliance team's favorite page
if you have one.

---

## What's next

Chapter 25 covers *Help & Feedback* — version info, support
contacts, and the feedback inbox.
