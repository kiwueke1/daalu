# 4. Signing up and creating your tenant

This chapter takes you from "I want to try Daalu" to "I'm logged
in with a working tenant." It's the shortest chapter in the book —
the sign-in flow is deliberately short — but it's important to
understand what's happening so the rest of the system makes sense.

If you've already signed up and skipped ahead, jump to Chapter 5
for the home dashboard tour.

---

## What you'll create

By the end of this chapter you will have:

1. A **user account** — an identity associated with you
   personally, backed either by your Google account or by an
   email + password you choose.
2. A **tenant** — the workspace that your user belongs to. Your
   integrations, your alerts, your data all live here. Everyone you
   later invite will share this same tenant.

If your company already has a Daalu tenant and someone has invited
you, you'll receive an invite link instead. Use that link rather
than creating a fresh account — joining the right tenant matters
and is hard to fix later.

---

## The sign-in page

Open a browser to **`https://ops.example.com`**. You'll land on the
**Sign in** screen. Depending on how your Daalu instance is
configured, you'll see up to three ways in:

- **Continue with Google** — single sign-on. Click it, pick your
  Google account, and you're in. There's no separate Daalu
  password to manage.
- **Email + password** — the local form, for accounts created by
  an operator, via an invite, or through self-service sign-up.
- **Create an account** — the self-service sign-up link (only
  shown where the instance has it enabled).

### One login, everywhere

Your Daalu session is a real single sign-on session. It doesn't
just open the Daalu app — it also signs you into the consoles of
the managed services Daalu provisions for you, such as the
network management stack's source-of-truth and workflow UIs
(Chapter 27). Log in once in the morning; every console you open
from Daalu already knows who you are.

---

## Step-by-step: self-service sign-up

Where enabled, click **Create an account** on the sign-in page
(or go to `/signup` directly). The form asks for:

- **Full name.** Shown in the UI to your teammates and on change
  proposals you approve.
- **Work email.** Use a real, deliverable address — the
  verification link goes there.
- **Organisation** (optional). Your company name, for your own
  reference.
- **Password** and **Confirm password.** At least 8 characters.
  The form tells you as you type how many more characters you need
  and whether the two passwords match. Daalu stores only a strong
  hash — we never see the plaintext.

Click **Create account**. You'll see a **"Check your email"**
screen; the verification email (from your deployment's sender address, e.g. `noreply@example.com`) arrives
within a minute. Click the link in it to activate the account,
then sign in. If it doesn't arrive, check spam, then click
**resend the link** on the same screen.

You start in **your own private workspace** — a fresh, fully
isolated tenant containing just you.

> **Don't see "Create an account"?** Self-service sign-up is a
> per-instance setting and your operator may not have enabled it.
> In that case accounts are created by invite — ask your admin.

---

## Step-by-step: joining via invite

If someone in your company invites you (Chapter 6), you'll get an
email from your deployment's sender address with a join link. Click it — the
accept-invite page works before you're signed in, sets your
password, and lands you directly in the inviting company's
tenant.

- You can't change the tenant — you're joining an existing one,
  not creating your own.
- Your role is set by whoever invited you — typically "user"
  rather than "admin."

The invite link is single-use and expires after 7 days. If it
expires, ask the person who sent it to issue a new one from
*Settings → Team*.

---

## What you'll see on first login

After your first login you land on the home page (Chapter 5). On
a new tenant it's quiet: the briefing says there's nothing yet and
offers to generate one or connect a source.

The guided setup lives at *Managed infra → Onboarding*. It sets up
notifications, observability and the cluster tunnel in one
sequence, and you can skip any step you don't need. A good first
hour looks like this:

1. **Connect a notification channel.** Slack or email, from the
   Onboarding tab or *Managed infra → Integrations*.
2. **Connect at least one source.** A cloud account, an
   observability stack, or a Kubernetes cluster — anything that
   gives Daalu something to look at.
3. **Check the AI Assistant works.** Open **Assistant** in the
   right rail and ask anything; it should respond.
4. **Invite a teammate** from *Settings → Team*. Daalu is much
   more useful with two people in the workflow than with one.

Don't worry if you can't do all four right now — connecting a
cloud account properly might require coordination with your IT
team. None of these steps blocks the others.

---

## A note on tenants and signing up twice

Two situations come up that confuse people:

### "I signed up but my colleague is already on Daalu"

If your colleague created the company's tenant before you, you
should have used their invite instead of self-service sign-up.
The result is that you now own a **separate tenant** with just
you in it.

The fix: ask your colleague to invite you to *their* tenant. Once
you accept, your two-account situation resolves cleanly — you'll
have a personal one-user tenant (which you can leave abandoned)
and a membership in the real company tenant.

### "I want to manage two different companies' Daalu instances"

That works fine. Each company has its own tenant; use the
matching account for each.

---

## What's happening behind the scenes

For the technically curious — and because this affects how the
rest of the book reads — here's what just happened when you
signed in for the first time:

- Your identity was established — either verified by Google
  through single sign-on, or by the email-verification link you
  clicked.
- A user record was created and bound to a tenant (a fresh one
  for self-service sign-up; the inviting company's for an
  invite).
- A session was issued to your browser. That same session is
  honoured by the managed-service consoles Daalu runs for your
  tenant — that's the "one login, everywhere" behaviour above.

Future teammates join the *same* tenant via an **invite** — a
one-shot emailed link that locks the new user to the correct
tenant.

---

## What's next

You're now logged in with a fresh tenant.

Chapter 5 is a guided tour of the home page — the panels you're
looking at, what each one means, and what the system is showing
you before you've connected anything.

Chapter 6 covers inviting your team, which you can do at any
point. Inviting teammates early means they can investigate the
first real incident with you instead of being onboarded mid-fire.

Chapter 7 walks you through connecting your first cloud account.
This is the most common first integration and the one that lights
up the most of the UI.
