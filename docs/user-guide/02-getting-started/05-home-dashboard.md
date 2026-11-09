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

The page is divided into three columns, with a bar across the
top:

```
+---------+----------------------+-------+
|         | top bar              |       |
+---------+----------------------+-------+
| sidebar | main column          | rail  |
| (nav)   |                      |       |
|         |  - greeting hero     | Asst. |
|         |  - daily briefing    | Alerts|
|         |  - summary tiles     | Sess. |
|         |                      | Health|
+---------+----------------------+-------+
```

- **Left sidebar** — the global navigation, grouped by what you
  came to do. Every other page in this book is reached from here.
  It stays on screen as the page scrolls.
- **Top bar** — the alert status, an *Ask anything about
  operations…* box, and your account menu. If automation is paused
  or a circuit breaker has tripped, it says so here, on every page.
- **Main column** — what's happening in your tenant right now.
  The full daily briefing sits at the top so you don't have to
  click through to read it.
- **Right rail** — a slim strip with the AI Assistant, your alert
  feed, active automation sessions, and a system-health gauge. Each
  opens when you click it. We cover the Assistant properly in
  Chapter 12.

---

## The greeting hero

The top of the main column is a soft, glowing hero panel with:

- "Good morning, *Name*." (Or afternoon / evening based on your
  local time.)
- A one-line operational summary — "No open alerts. Everything is
  running smoothly." or "*N critical alerts* need attention" when
  something is firing. That phrase is a link to the open alerts.
- The Daalu AI core illustration.

Apart from that link, the hero is a status card, not a button.

---

## The daily briefing

Directly beneath the hero sits the **AI briefing** card. It opens
with a one-line headline summary, a row of **metric chips** (the
day's key numbers — open alerts, events, proposals — each as a
big figure with a small label, under a note saying they cover
the briefing's own window), and then a grid of **clickable
section tiles**, one per briefing section: typically *Last 24
hours*, *Active incidents*, *Recommended actions*, and *Capacity
& cost*. Each tile shows an icon, its title, and a one-glance
preview line; click it and it expands in place to reveal the full
section. That way a quiet day reads in five seconds, and a busy
day lets you drill into exactly the section that matters.

The briefing is written by an agent on a schedule — by default
every weekday at 07:00 UTC. The schedule is set for your Daalu
instance rather than in the app; to change it, ask your operator. The agent reads every event in the window it
covers, for example:

- Recently-resolved and currently-firing alerts.
- Drift and other activity from the network reconciler.
- Alerts and events from your connected clouds and monitoring.
- Change proposals opened or waiting for approval.

The card has a **Regenerate** button in its top-right corner.
Click it to ask the agent to write a fresh briefing from current
data — useful when something just happened and you don't want to
wait for tomorrow's scheduled run. The button reads *Writing a new
briefing…* while it works, and a short message confirms when the
new briefing is in (or says why it couldn't be written).

On a brand-new tenant the briefing will be honest about not
having much to say: *"No briefing yet for the infra channel."*
Below that, it tells you to click **Generate today's briefing** to
write one now, or to **connect a source** (a link to *Managed
infra → Integrations*) to give it more to work with.

Historical briefings are archived on the *Reports* page (Chapter
23), under the **Briefings** tab. Click any past briefing in that
list to re-read it.

---

## Summary tiles

Beneath the briefing is a grid of small tiles, each showing a
single signal and linking through to the relevant detail page.
Each tile names its own time window, because they are live counts
and the briefing covers its own window — the two needn't match.

- **Open now** — alerts open right now, with how many are critical
  and any open incidents. Green hint if nothing is critical; red if
  something is. Links to the *Alerts* page, open tab.
- **Seen in last 24h** — distinct alerts plus events from the last
  day, with a tiny sparkline of the 24 hours. Links to *Alerts*.
- **Workflows running** — workflow runs in flight or waiting for
  approval, with how many completed today. Links to *Workflows*.
- **Active agents** — count of autonomous helpers currently
  online. Links to the *Agents* page.
- **Briefings on file** — archived briefing count. Links to
  *Reports*.

Each tile is a single click target. On a phone the tiles sit in
one row you swipe sideways.

---

## The right rail

On a wide screen, a slim strip runs down the right side of most
pages (not just home), with four sections: **Assistant**,
**Alerts**, **Sessions** and **Health**. They start closed so the
page gets the width; click one to open it, and click again to close
it. Your choice is remembered in this browser. A dot on **Alerts**
shows when something is firing.

On phones and smaller windows the strip becomes an **Assistant**
button in the bottom-right corner, showing the number of open
alerts. It opens a sheet with **Ask**, **Notifications** and
**System health** tabs.

### AI Assistant

The chat input is always one click away. Type a question and the
Assistant responds with grounded answers — citing the metrics,
logs, or events it drew on. Three suggested prompts appear when
the conversation is empty:

- "What services are degraded?"
- "Summarise today's events."
- "Which alerts need attention?"

The conversation is kept while you move between pages, and starts
fresh when you reload.

### Alerts

Labelled **Alerts** on the strip and **Notifications** once open:
a live feed of three groups:

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
each in its own chapter in Part IV. The sidebar is grouped by what
you came to do, most urgent first.

- **Home** — what you're on now.

**Respond**

- **Alerts** — open and resolved alerts (Chapter 20).
- **Investigations** — every alert the AI investigated, where you
  grade its root cause as correct, partly right or wrong.
- **Workflows** — multi-step automations, with run history
  (Chapter 21).

**Automate**

- **Automations** — workflows you've authored, with live run
  status.
- **Agents** — autonomous helpers and their run history
  (Chapter 19).
- **Runbooks** — your team's fixes, written down where the AI can
  use them when it triages a matching alert.
- **Autonomy policy** — decides, for every change the AI wants to
  make, whether it is denied, waits for approval, or runs on its
  own (Chapter 52).

**Infrastructure**

- **Managed infra** — clusters & observability, the network
  management stack, guided onboarding, and integrations — all
  as tabs on one page (Chapter 17). This is also where the old
  standalone *Integrations* page now lives.
- **AI Factory** — GPU observability and operations
  (Chapter 50).
- **Operations** — the SoT hub: devices, drift, proposals, and
  bulk import as tabs (Chapter 18).
- **Workspace** — the browser coding assistant (Chapter 26).

**Insights**

- **Dashboards** — your Grafana dashboards, rendered natively
  (Chapter 48).
- **Reports** — archived briefings (default tab) and saved
  analyses / ad-hoc queries (Chapter 23).
- **Maturity** — where your team sits on the AI operations
  maturity model (Part IX).
- **Evals** — break something on purpose, let the AI investigate,
  and grade what it did.
- **Audit log** — every action taken, by a person or by the
  autonomy policy.

At the very bottom of the sidebar:

- **Usage & Pricing** — spend and inference cost (Chapter 22).
- **Settings** — profile, preferences, API tokens, notification
  routing.
- **Help & Feedback** — version info and a way to write to the
  team that builds Daalu.

When something needs you, it shows on the entry itself, from any
page: critical alerts or warnings on **Alerts**, investigations to
grade on **Investigations**, runs waiting for approval on
**Workflows**, and **Paused** on **Autonomy policy** when automation
is stopped.

The live network map (Chapter 49) isn't a sidebar entry. It belongs
to one network environment, so you open it from *Managed infra →
Network & servers → your environment → Consoles*.

---

## What a populated home page looks like

For comparison, here's roughly what the page looks like a month
into a real customer's tenant:

- Greeting: "Good morning, Jane. 1 critical alert needs
  attention (3 open in total)."
- Briefing: three paragraphs. The first summarizes overnight
  cloud activity ("EC2 instances scaled in by 4; one new RDS
  snapshot retained from the daily job"). The second covers
  alerts ("`kafka-broker-disk-pressure` fired at 03:12 and
  resolved at 03:18 — agent traced it to a temporary log
  rotation spike"). The third lists pending decisions
  ("ChangeProposal #4317 from the SoT reconciler wants approval
  — a new BGP neighbor on edge-router-3").
- Summary tiles: real numbers — 3 open now (1 critical), 1,247
  seen in the last 24 h, 2 workflows running, 4 active agents,
  31 archived briefings.
- Sidebar: "1 critical" on Alerts, "1 to approve" on Workflows.
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
