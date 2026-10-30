# 6. Inviting your team

Daalu is a multiplayer tool. A change proposal needs an approver
that isn't its author. An alert needs an owner. The Assistant's
suggestions need a human reviewing them. None of that works with a
tenant of one.

This chapter covers how to invite teammates, what roles exist,
what each role can do, and how to remove someone when they leave.

---

## Inviting someone

From the sidebar: **Settings → Team → Invite user.**

A dialog appears asking for:

- **Email address.** Their work email. We send the invite there.
- **Role.** Two choices today: **admin** or **user**. We'll
  explain the difference below.
- **Optional message.** A short note that's included in the
  invite email. Useful for "hey, here's the Daalu account I
  mentioned at standup" context.

Click **"Send invite."** The invitee receives the invite email
(from your deployment's sender address, e.g. `noreply@example.com`) within a minute, and a confirmation
banner shows you the invite link as well — handy if you'd
rather drop it into Slack yourself, or if their mail filter is
being difficult.

The invite is a single-use link that expires after **7 days**. If
they don't click it in time, come back to *Settings → Team*, find
the pending row, and click **"Resend."** A resent invite generates
a new token; the old one is invalidated, and the new link is
shown in the banner again.

You can also see all pending invites in the *Active invites*
section of the *Settings → Team* page so you don't double-invite
someone. Toggle "Show all" to include accepted and revoked invites
for audit.

---

## Roles: admin vs user

Daalu has exactly two roles today. We keep the model deliberately
small.

### admin

Admins can:

- Invite and remove users.
- Change other users' roles (promote a user to admin, demote an
  admin to user).
- Add, edit, and remove integrations.
- Manage tenant settings (name, time zone, notification defaults).
- Mint and revoke personal access tokens for *any* user, not just
  themselves.
- Approve change proposals — including ones they didn't author.
- View the *Billing* page and download invoices.

The tenant must always have **at least one admin**. The UI
prevents you from demoting the last one.

### user

Users can:

- Investigate everything — read alerts, browse devices, query the
  Assistant.
- Open change proposals.
- Approve change proposals they didn't author (but a proposal
  cannot be self-approved).
- Mint and revoke their own personal access tokens.
- Edit their own profile and preferences.

Users cannot:

- Invite or remove other people.
- Change anyone's role.
- Add or remove integrations.
- See the *Billing* page.

These restrictions are enforced at the API level — calling the
backend directly with a non-admin token will hit the same gate
the UI does.

### What we deliberately don't have (yet)

A few things our customers have asked about that we *don't* have:

- **Per-resource permissions** ("user X can see cluster A but
  not cluster B"). The whole tenant is shared.
- **Read-only roles.** "User" is investigative-write — they can
  open proposals. We've seen no compelling pattern for a true
  read-only role that's different from "let them have an
  account."
- **Custom roles.** Admin and user is what we have.

We may add some of these. The customer-success team takes
requests; reach out from *Help & Feedback*.

---

## What "approval" actually means

Throughout this book you'll see references to approving change
proposals. A few clarifications, since the rules surprise people:

- **Self-approval is blocked.** The user who authored or proposed
  a change cannot approve it. This includes the Assistant — if
  the Assistant proposes a change at your behest, it counts as
  your proposal and you can't approve it. This is the only
  enforced four-eyes rule in Daalu.
- **One approval is enough.** A change with one approver other
  than the proposer can execute. We don't currently support a
  policy like "two reviewers for production proposals."
- **Approval can be revoked** while a proposal is queued for
  execution. Once execution starts, approval is locked.

If you want stricter rules (e.g., always require two approvers
for production changes), the workflow tools in Part IV Chapter 21
let you encode that. The team-level role system is intentionally
simple.

---

## Removing a user

From *Settings → Team*, find the user's row and click the **⋯**
menu. Choose **"Remove from tenant."**

What happens immediately:

- The user's session is invalidated. If they're logged in, their
  next page click will dump them at the login screen with
  "Account no longer has access."
- All of their personal access tokens are revoked.
- Their pending proposals remain — they're labelled "proposed by
  <removed user>" and can still be approved or denied by others.
- Their authored automations remain.

What happens 30 days later, automatically:

- A nightly job hard-deletes the user row.
- Audit-log entries that reference them keep their email but lose
  the foreign-key link.

If you re-invite the same email within 30 days, the user is
restored and their pre-existing artifacts re-link to them. After
30 days, the new account is fresh.

This 30-day grace period exists for the very common case of
"oops, I removed the wrong person."

---

## Transferring tenant ownership

There isn't a formal "owner" — only the admin role. If you're the
sole admin and you want to leave the tenant:

1. Promote at least one other person to admin first.
2. *Settings → Team*, find your own row, click **⋯ → Leave
   tenant.**

If you're the sole admin and *also* the sole user, the tenant
has no good landing state. You should either:

- Invite a teammate and then leave, or
- Delete the tenant entirely from *Settings → Tenant → Delete*.

Tenant deletion is irreversible and immediately purges all data.
We require typing the tenant name to confirm.

---

## When someone uses Daalu from CI or scripts

If you're inviting an account that isn't really a person (a
shared "automation" account, or a service identity), don't bother
giving it a password — instead:

1. Invite the account with a temporary password.
2. Log in once to claim the invite and set a real password.
3. Generate a personal access token from *Settings → API
   tokens*. (Chapter 37 covers PATs in detail.)
4. Use the PAT in your scripts. Discard the password.

The PAT is the durable credential. Passwords on automation
accounts get rotated by people who don't know they're being used
by a CI pipeline, and the breakage is hard to debug.

---

## What's next

You can now invite teammates as needed.

Chapter 7 walks through connecting your first cloud account.
Bring at least one teammate along for that exercise — having a
second pair of eyes the first time you grant Daalu access to a
real AWS or GCP account is just good hygiene.
