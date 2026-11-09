# 22. Billing and spend

The *Usage & Pricing* page puts your local GPU, your plan, and
what you've spent on AI inference on a single screen, so the
answer to "what does running Daalu cost?" is one click away.

URL: `https://ops.example.com/billing`. Everyone in the tenant can
see it; only admins can switch plans.

---

## Layout

The page header reads *"Where your tokens are made, what you pay
for them, and which plan you are on."* From top to bottom:

1. **Local GPU status** (top right) and the **Local GPU** card
2. **Current period**
3. **Plans**
4. **30-day spend**
5. **Where tokens were made** and **Top sources this period**

We'll cover each.

---

## Local GPU

### Status pill

- **Online** — your local GPU is answering.
- **Deploying…** — Daalu is rolling the serving stack out onto
  your cluster; the collapsible **Deploy progress** panel shows
  the rollout live (pod state, blocking reasons, cluster events,
  and the serving logs once they exist).
- **Offline** — it stopped answering. Probably the serving pod is
  unhealthy or the cluster tunnel is down — the same Deploy
  progress panel is offered here too, because its events and logs
  are how you find out why.
- **No GPU connected** — the page says requests go to external
  models and are billed at external rates, with a link: **Connect
  a GPU in AI Factory**. You can also start from **Connect your
  GPU** on the card here; chapter 35 walks through it.

The pill and the AI Factory page share one health probe, so
they always agree.

To stop serving from your GPU, click **Disconnect GPU…**. It asks
you to confirm in place (**Disconnect GPU**) and says what happens:
the model is removed from the GPU, and inference falls back to
external models, billed at external rates.

---

## Current period

What you've spent this billing period: the total, split into
**base** (your plan) and **usage**, the plan you're on, and three
figures — **Events**, **Tokens in** and **Tokens out**. If your
plan includes a bundle of events, a bar shows how much of it
you've used.

---

## Plans

One card per plan, each with its routing policy (for example
*"Local GPU first; external models for hard questions"*) and its
price per million tokens for each tier. Your plan reads *Your
current plan*. On any other, the **Switch to …** button asks you
to confirm in place and states the price that applies from today.
After you confirm, a short message says you're on the new plan.

---

## 30-day spend

A chart of what you've spent per day over the last 30 days.

---

## Where tokens were made, and top sources

- **Where tokens were made** — your usage split by tier: *Local
  GPU*, *External classifier*, *External quality*, and
  *Sovereign*. The first time you see this on a tenant with local
  inference turned on, a large *Local GPU* share is usually a
  happy moment.
- **Top sources this period** — which parts of Daalu used the
  most.

With no usage yet this period, the panels say so.

---

## What's *not* on this page

- **Cloud spend.** Daalu doesn't read your AWS, GCP or Azure
  bills; this page is about what Daalu costs you. Use your cloud
  provider's cost tools for cloud spend.
- **Invoices and payment details.** Ask your operator.
- **Exports.** There's no export button. Your own scripts can read
  the same figures through the API with a personal access token
  (Chapter 37).
- **Per-user usage.** The page shows the tenant as a whole.

---

## What's next

Chapter 23 covers the *Reports* page — AI-generated briefings
plus a query tab for ad-hoc and saved analyses.
