# 19. Agents

The Daalu **Agents** page is where you see the autonomous helpers
running in your tenant. Each agent is a small program that
listens for events and has a focused job — for example,
triaging every alert that fires.

URL: `https://ops.example.com/agents`.

This is one of the most powerful pages once you've been using
Daalu for a few weeks. New customers often don't notice it
exists. By month two, it's where they live.

---

## What an agent is

An agent is:

- A named behaviour that belongs to a module.
- Subscribed to a set of event types (an alert firing, an alert
  resolving, a deployment failing, a capacity warning…). When one
  of those events arrives, the agent runs.
- Subject to the same approval rules as the Assistant. If what it
  wants to do changes something, it goes through an approval card
  or a change proposal that a human approves, as your autonomy
  policy decides.

Agents start with the modules your tenant has; you don't create
or configure them in the app.

---

## What runs for you

### The infra agent (on the Agents page)

Triages firing alerts, opens incidents for serious ones, drafts
likely root causes and remediation steps for the on-call SRE,
closes alerts when they stop firing upstream, and recommends
capacity actions. Its card on the Agents page shows how it's
doing. You met its work on every alert's detail page.

### Scheduled jobs (not on the Agents page)

Some of the work Daalu does for you runs on a schedule rather than
as an agent, so it doesn't have a card. You see its results
elsewhere:

- **Daily briefing** — weekdays at 07:00 UTC by default; it shows
  on Home and under *Reports → Briefings* (Chapter 23).
- **Source-of-truth reconciler** — every 5 minutes; results on
  *Operations → Drift* and *Routine runs* (Chapter 18).
- **Alert ingest** — pulls firing alerts from Prometheus and AWS
  CloudWatch every minute or two (Chapter 11).
- **Cluster tunnel health** — every 30 seconds; the status on each
  cluster's row (Chapter 14).
- **Integration health checks** — every minute; the status badge
  on each integration (Chapter 10).

These schedules are the same for every tenant on your Daalu
instance. If one needs changing for you, ask your operator.

---

## The Agents page layout

The page is a grid of cards, one per agent:

Each card shows:

- The agent's **name**, the module it belongs to, and a one-line
  description.
- A **status** chip from its last run — `ok`, `error`, or `idle`
  if it hasn't run yet.
- Three figures: **Success rate**, **Runs (24h)**, and **Last
  seen**.
- **View failed runs** (only when something failed) — unfolds the
  last few failures, each with when it happened and the error
  message, so you can see *why* without leaving the page.
- **Details** — unfolds the event types the agent listens to.

With no agents running, the page says so and explains that agents
start with the modules you enable.

---

## Turning agents off, and changing what they do

There are no per-agent switches or settings in the app. What you
*can* control:

- **What the AI may change on its own** — your autonomy policy
  (*Automate → Autonomy policy*, Chapter 52).
- **How it fixes things** — runbooks (*Automate → Runbooks*,
  Chapter 54). A published runbook that matches an alert goes into
  the agent's triage context.
- **Which sources feed it** — the integrations you connect
  (Chapter 17).

To pause an agent for your tenant, ask your operator.

---

## Automations you can run yourself

For one-off jobs you start by hand — such as **Coordinate an
incident** — see the *Automations* page (Chapter 21).

---

## When an agent fails

Agents fail like any other program. The most common modes:

- **Tool error.** A downstream system returned an error (cloud
  API throttled, Loki unreachable). The run is marked failed
  with the error message, and the agent runs again on the next
  matching event.
- **Missing integration.** The agent needed a source you haven't
  connected (for example, no Prometheus for a metrics question).
  Connect it on *Managed infra* (Chapter 17).
- **Waiting on you.** A fix it proposed is still waiting for
  approval. The sidebar shows the count on **Workflows**, and the
  alert's header shows **Review plan**.

The **Success rate** figure on each agent's card is the best
place to spot trending issues, and **View failed runs** shows the
error behind each failure. A green 100 % means the agent is happy.
A 70 % means you should investigate.

---

## What's next

Chapter 20 covers *Alerts*. The Assistant and several agents
interact with alerts heavily; the page itself has its own UX
worth touring.
