# 5. Your first five minutes on the home dashboard

The home dashboard is the page Daalu opens to when you log in.
It's at `https://ops.example.com/` — no path. Spend a few minutes
here on your first day and you'll already know where everything
else lives.

This chapter walks the page from top to bottom, panel by panel.
Where a panel needs data Daalu doesn't have yet, we explain what
you'd need to connect to populate it.

---

## The layout

The page is divided into three columns:

```
+---------+----------------------+----------+
| sidebar | main column          | rail     |
| (nav)   |                      |          |
|         |  - greeting hero     |  - AI    |
|         |  - daily briefing    |  - notifs|
|         |  - summary tiles     |  - active|
|         |                      |  - health|
+---------+----------------------+----------+
```

- **Left sidebar** — the global navigation. Every other page in
  this book is reached from here. Once you've logged in once or
  twice, you'll know it by muscle memory.
- **Main column** — what's happening in your tenant right now.
  The full daily briefing sits at the top so you don't have to
  click through to read it.
- **Right rail** — the AI Assistant, your notification feed,
  any active automation sessions, and a system-health gauge. We
  cover the Assistant properly in Chapter 12.

---

## The greeting hero

The top of the main column is a soft, glowing hero panel with:

- "Good morning, *Name*." (Or afternoon / evening based on your
  local time.)
- A one-line operational summary — "All systems are running
  smoothly" or "*N items* need review" if criticals are open.
- Subtle ambient particles and the Daalu AI core illustration.

Click anywhere outside an interactive element to leave the hero
untouched — it's a status card, not a button.

---

## The daily briefing

Directly beneath the hero sits the **AI briefing** card. It opens
with a one-line headline summary, a row of **metric chips** (the
day's key numbers — open alerts, events, proposals — each as a
big figure with a small label), and then a grid of **clickable
section tiles**, one per briefing section: typically *Last 24
hours*, *Active incidents*, *Recommended actions*, and *Capacity
& cost*. Each tile shows an icon, its title, and a one-glance
preview line; click it and it expands in place to reveal the full
section. That way a quiet day reads in five seconds, and a busy
day lets you drill into exactly the section that matters.

The briefing is composed by an agent that runs once a day at your
configured **briefing time** (default: 8 AM in your tenant's time
zone). It pulls from:

- Recently-resolved and currently-firing alerts.
- New devices discovered by the network reconciler.
- New cloud resources detected (e.g., "12 new EC2 instances came
  up in us-east-1 yesterday").
- Open change proposals waiting for approval.
- Anomalies in spend that exceed the dollar threshold set in
  *Settings → Briefings*.

The card has a **Regenerate** button in its top-right corner.
Click it to ask the agent to write a fresh briefing from current
data — useful when something just happened and you don't want to
wait for tomorrow's scheduled run.

On a brand-new tenant the briefing will be honest about not
having much to say: *"No briefing yet for the infra channel. To
generate one now, click Regenerate. To feed real data in,
configure an integration on the Integrations page or POST events
to /api/v1/events."*

Historical briefings are archived on the *Reports* page (Chapter
23), under the **Briefings** tab. Click any past briefing in that
list to re-read it.

---

## Summary tiles

Beneath the briefing is a grid of small tiles, each showing a
single signal and linking through to the relevant detail page:

- **Active agents** — count of autonomous helpers currently
  online. Links to the *Agents* page.
- **Open incidents** — current open incidents with a critical
  count. Green hint if it's all clear; red if it isn't. Links
  to *Operations*.
- **Operational activity (24h)** — count of events ingested in
  the last day, with a tiny sparkline showing the 24-hour
  histogram. Links to *Alerts*.
- **Workflows running** — automation sessions in flight, with
  how many completed today. Links to *Automations*.
- **Briefings on file** — archived briefing count. Links to
  *Reports*.

Each tile is a single click target. Hover to see the link target
in the browser status bar.

---

## The right rail

Anchored to the right side of every page (not just home):

### AI Assistant

The chat input is always one click away. Type a question and the
Assistant responds with grounded answers — citing the metrics,
logs, or events it drew on. Three suggested prompts appear when
the conversation is empty:

- "What services are degraded?"
- "Summarise today's events."
- "Which alerts need attention?"

Conversations persist per user across sessions.

### Notifications

A live feed of three groups:

- **Critical** — open critical alerts (newest first).
- **Warning** — open warnings.
- **AI Generated** — pending recommendations the agent has
  surfaced for review.

**Every notification is clickable.** A critical or warning item
opens the alert's detail page (e.g.,
`/alerts/c5936f76-…`). A recommendation item lands on the
*Proposals* page where you can review and approve or dismiss.

### Active sessions

Workflow / automation runs currently in flight. Each row shows
the workflow name, when it started, and whether it's running or
waiting for approval. Useful when the AI is doing something on
your behalf and you want to know about it.

### System health

A small radial gauge at the bottom showing percentage
reliability — computed from the count and severity of open
alerts. 100 % means nothing is firing.

---

## The sidebar in detail

Quickly, what each item in the left sidebar does. We'll cover
each in its own chapter in Part IV.

- **Home** — what you're on now.
- **Operations** — the SoT hub: devices, drift, proposals, and
  bulk import as tabs.
- **Agents** — autonomous helpers and their run history.
- **Automations** — workflows you've authored, with live run
  status.
- **Reports** — archived briefings (default tab) and saved
  analyses / ad-hoc queries.
- **Alerts** — open and resolved alerts.
- **Workflows** — multi-step automations, with run history.
- **Workspace** — the browser coding assistant (Chapter 26).
- **AI Factory** — GPU observability and operations
  (Chapter 50).
- **Topology** — the live network fabric map (Chapter 49).
- **Dashboards** — your Grafana dashboards, rendered natively
  (Chapter 48).
- **Managed infra** — clusters & observability, the network
  management stack, guided onboarding, and integrations — all
  as tabs on one page (Chapter 17). This is also where the old
  standalone *Integrations* page now lives.
- **Usage & Pricing** — spend and inference cost.

At the very bottom of the sidebar:

- **Settings** — profile, preferences, API tokens, notification
  routing.
- **Help & Feedback** — version info and a way to write to the
  team that builds Daalu.

---

## What a populated home page looks like

For comparison, here's roughly what the page looks like a month
into a real customer's tenant:

- Greeting: "Good morning, Jane. 2 items need review."
- Briefing: three paragraphs. The first summarizes overnight
  cloud activity ("EC2 instances scaled in by 4; one new RDS
  snapshot retained from the daily job"). The second covers
  alerts ("`kafka-broker-disk-pressure` fired at 03:12 and
  resolved at 03:18 — agent traced it to a temporary log
  rotation spike"). The third lists pending decisions
  ("ChangeProposal #4317 from the SoT reconciler wants approval
  — a new BGP neighbor on edge-router-3").
- Summary tiles: real numbers — 4 active agents, 1 open
  incident, 1,247 events in the last 24 h, 2 workflows running,
  31 archived briefings.
- Right rail: 1 critical, 2 warnings, all clickable; AI
  Assistant ready; 1 workflow waiting for approval; 96 %
  reliability.

On a brand-new tenant the page reads like a quiet morning. That
is expected and not a bug — connect a source under *Managed
infra → Integrations* and signals will start flowing.

---

## What's next

Chapter 6 covers inviting your team. Even if you're not ready to
do this on day one, glance at the chapter — there are a few
gotchas (role choices, what happens on offboarding) that are
useful to know before they bite you.

Chapter 7 walks through connecting your first cloud account,
which is what makes the home page actually have something to say
tomorrow.
