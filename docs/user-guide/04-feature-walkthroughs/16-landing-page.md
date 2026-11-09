# 16. The Landing page

Chapter 5 introduced the home dashboard at a conceptual level.
This chapter is the reference: every panel, every button, every
column header on the home page, explained in order.

It's a chapter you can skip on first read and come back to when
you want to know what some specific element on the page does.

---

## URL

`https://ops.example.com/` — no path. Also reached from **Home** at
the top of the sidebar on any page.

---

## Top bar (every page)

The bar at the very top is shared by every page:

- **Menu button** (phones only). Opens the sidebar as a drawer.
- **Alert status** (left). "No open alerts", "2 warnings" or
  "1 critical alert", coloured to match. Click to open *Alerts*.
- **Automation status.** Only shown when it matters: *Automation
  paused*, or *N breakers tripped* when a circuit breaker has
  switched an automatic rule back to needing approval. Click to
  open *Autonomy policy*.
- **Ask box** in the middle — *Ask anything about operations…*.
  Type a question and press Enter for an answer right under the
  box, or pick one of the suggested questions. `Cmd-K` / `Ctrl-K`
  focuses it from anywhere; `Escape` closes the answer.
- **Theme toggle.** Switches between light and dark. By default
  Daalu follows your operating system's setting.
- **Bell.** Opens *Alerts*. A dot shows when something is firing.
- **Account menu** (top-right, your initial). Shows your name and
  email, and **Sign out**.

---

## Left sidebar (every page)

Same on every page, and grouped by what you came to do:

- **Home**
- **Respond:** Alerts, Investigations, Workflows
- **Automate:** Automations, Agents, Runbooks, Autonomy policy
- **Infrastructure:** Managed infra, AI Factory, Operations, Workspace
- **Insights:** Dashboards, Reports, Maturity, Evals, Audit log
- At the bottom: Usage & Pricing, Settings, Help & Feedback

What needs you shows on the entry itself, from any page: critical
alerts or warnings on **Alerts**, investigations waiting for a verdict
on **Investigations** ("2 to grade", over the last 30 days), workflow
runs waiting for approval on **Workflows** ("1 to approve"), and
**Paused** on **Autonomy policy** when automation is stopped.

The sidebar stays on screen as a page scrolls. On phones it becomes a
slide-in drawer behind the hamburger button. Devices and Proposals are tabs
inside *Operations*; Integrations, Clusters, and Onboarding are
tabs inside *Managed infra* — their old standalone URLs still
resolve.

---

## Main column on the Landing page

In order from top to bottom:

### 1. Greeting

"Good morning, Jane." (Or afternoon / evening based on your local
time.) A small label above it reads *AI operating · Morning
summary*. Below the greeting, one status line:

- "No open alerts. Everything is running smoothly." when nothing
  is firing.
- "*N critical alerts* need attention" (or "*N open alerts*" when
  none are critical), followed by the total open count when it's
  higher. The phrase links to the open alerts.

### 2. AI briefing

The generated daily briefing, presented as:

- a **headline summary** line (long headlines are shortened; hover
  for the full text, and **Read the full summary** unfolds the rest),
- **metric chips** — the day's key figures as big numbers with
  small labels, under a note that they cover *this briefing's
  window*,
- and a grid of **clickable section tiles** (typically *Last 24
  hours*, *Active incidents*, *Recommended actions*, *Capacity
  & cost*), each showing an icon and a one-line preview.
  Clicking a tile expands it in place to reveal the section's
  full text.

Inputs include alerts that fired/resolved overnight, new cloud
resources detected, open change proposals, spend anomalies, and
agent-generated highlights.

Click **Regenerate** to rerun with fresh data (it reads *Writing a
new briefing…* while it works), or **Open the full briefing** to
open it as a page (past briefings live under *Reports →
Briefings*).

### 3. Summary tiles

Five tiles, each a link. Each names its own time window, since
they are live counts and needn't match the briefing's figures:

- **Open now** — open alerts, with the critical count and any open
  incidents. Opens *Alerts* on the open tab.
- **Seen in last 24h** — distinct alerts plus events from the last
  day, with a sparkline. Opens *Alerts*.
- **Workflows running** — runs in flight or waiting for approval,
  and how many completed today. Opens *Workflows*.
- **Active agents** — opens *Agents*.
- **Briefings on file** — opens *Reports*.

On a phone the tiles sit in one row you swipe sideways.

Notifications, active sessions and system health aren't in the main
column; they live in the right rail, below.

---

## Right rail — Assistant, alerts, sessions, health

On a wide screen, a slim strip runs down the right side of most pages,
with four sections you open and close independently. They start closed
so the page gets the width, and your choice is remembered in this browser.
A dot on **Alerts** shows when something is firing.

- **Assistant** — ask about your live operational state, or start from a
  suggested question.
- **Alerts** — the critical and warning alerts, plus new AI recommendations.
- **Sessions** — workflows running or waiting for approval.
- **Health** — a reliability score based on open alerts.

On phones and smaller windows the strip becomes an **Assistant** button
in the bottom-right corner, showing the number of open alerts. It opens a
sheet with **Ask**, **Notifications** and **System health** tabs. Your
conversation is kept when you close the sheet or change page, until you
reload.

An alert's detail page replaces all of this with a chat about that alert.

---

## Keyboard shortcuts on this page

These work from any page, not just home:

- `Cmd-K` / `Ctrl-K` — focus the *Ask anything about operations…*
  box in the top bar.
- `Escape` — close the answer under the ask box, or close the
  sidebar drawer on a phone.
- `Tab` from the top of the page — the first stop is a **Skip to
  content** link that jumps past the sidebar.

---

## What you see when there's nothing connected

On a brand-new tenant the page is mostly empty states:

- Top bar and greeting: "No open alerts."
- Briefing: "No briefing yet for the infra channel.", with a
  **Generate today's briefing** button and a **connect a source**
  link.
- Summary tiles: zeros.
- Right rail: "No critical alerts.", "No warnings.", "No new
  recommendations." System health reads 100 % — vacuously true.

The empty states say what to do next rather than showing an error.
Click through to get the first signal flowing.

---

## What's next

Chapter 17 covers the *Managed infra* page — the inventory of
everything Daalu watches for you, and the place you go to add or
remove sources.
