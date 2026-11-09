# 24. Settings — profile, preferences, API tokens

The *Settings* page holds your personal settings — profile,
appearance, notifications and API tokens — plus your team's
invites. It's split into sections inside the page.

URL: `https://ops.example.com/settings`.

---

## Layout

**Settings** sits at the bottom of the sidebar, so it's reachable
from any page. The page has five sections:

- **Profile** — your own account.
- **Team** — who else has access, and invites.
- **Appearance** — theme and density.
- **Notifications** — what gets sent to you, where.
- **API tokens** — your personal access tokens.

The **Audit log** is not in Settings: it has its own sidebar entry,
under *Insights*.

If you leave a form with unsaved changes — closing or reloading the
tab — the browser warns you first. A short message confirms each
save, or says in plain words why it didn't work.

---

## Profile

- **Identity** — your **Email** (read-only: it's your sign-in, and
  only an admin can change it) and your **Display name**. **Save
  profile** is enabled once you've changed something.
- **Password** — **Current password**, **New password** and
  **Confirm new password**. As you type, the form tells you how
  many more characters you need (at least 8) and whether the two
  new passwords match; **Show passwords** reveals what you typed.
  **Change password** stays disabled, and says why, until all
  three are right.
- **Session** — **Sign out** of this browser. Other sessions,
  including API tokens, are unaffected.

---

## Appearance

- **Theme** — **Dark** or **Light**. Until you pick one, Daalu
  follows your operating system's setting. The same switch is in
  the top bar on every page.
- **Density** — **Comfortable** or **Compact**. Controls how much
  padding sits between rows in lists and forms.

Each change applies immediately.

---

## Notifications

**Channels** — what gets sent where. Defaults stay quiet; turn on
what you want:

- Email me on critical alerts.
- Email the daily briefing.
- In-app: a change proposal is ready for review.
- In-app: workflow failed.

---

## API tokens (PATs)

Personal access tokens are the durable credential for scripts
and CI. Chapter 37 covers how to use them; this page is the
management UI.

### Creating a token

Under **Mint a new token**:

- **Name** — for your future reference (e.g. `ci-runner`,
  `my-laptop`).
- **Expires in (days)** — leave blank for a token that never
  expires.

Click **Create token.** The cleartext token is shown **exactly
once**. Copy it now; you cannot retrieve it later. Paste it into
the secret store of whatever's going to use it.

### Token list

Under **Existing tokens**, each token shows its name, its prefix,
when it was created, when it was last used, and when it expires
(if set), plus whether it's revoked or expired.

### Token format

Tokens look like `dpat_xxxxxxxxxxxxxxxxxxxxxxxx`. The `dpat_`
prefix makes them easy to spot in env files and git diffs (and
makes secret scanners catch them if you commit by accident).
Use them as `Authorization: Bearer dpat_…` on any API call.

### Revoking

Click **Revoke** on the token. It asks you to confirm in place
(**Revoke token**) and says what happens: anything using this
token stops working immediately.

---

## Team

The same as Chapter 6 — pending invites, **Invite a teammate**,
resend and revoke. Only admins can invite people.

---

## Where other settings live

Some things you might look for in Settings live elsewhere:

| You want to… | Where |
|---|---|
| Connect Slack, email, clouds, monitoring | *Managed infra* (Chapter 17) |
| Decide what the AI may do on its own | *Automate → Autonomy policy* (Chapter 52) |
| Write down fixes for the AI to use | *Automate → Runbooks* (Chapter 54) |
| Schedule a report to Slack or email | *Reports → Schedules* (Chapter 23) |
| Switch plan | *Usage & Pricing* (Chapter 22) |
| Change alert rules | In your monitoring stack, e.g. Prometheus (Chapter 11) |

A few things are set by your operator rather than in the app:
the daily briefing schedule, the reconciler interval, LLM routing
overrides, removing users and changing roles, and deleting the
tenant. Ask your operator.

---

## Audit log

The audit log is its own page (Chapter 56) — *Insights → Audit log* in the
sidebar. It lists every read, proposal, approval, execution and
policy change, in order:

- Who did it (user, agent, executor).
- What was changed.
- When.
- Result.

Each event carries the hash of the one before it, so any edit or
gap breaks the chain; **Verify chain** checks it for you. The
compliance team's favorite page if you have one.

---

## What's next

Chapter 25 covers *Help & Feedback* — version info, support
contacts, and the feedback inbox.
