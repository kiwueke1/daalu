# 16. The Landing page

Chapter 5 introduced the home dashboard at a conceptual level.
This chapter is the reference: every panel, every button, every
column header on the home page, explained in order.

It's a chapter you can skip on first read and come back to when
you want to know what some specific element on the page does.

---

## URL

`https://ops.example.com/` — no path. Also reached by clicking the
Daalu logo in the top-left of any page.

---

## Top bar (every page)

The bar at the very top is shared by every page:

- **Daalu logo** (top-left). Click to return to the home page.
- **Tenant name** next to the logo. If you're in multiple
  tenants, click to switch.
- **Search input** in the middle. A unified search across
  alerts, devices, change proposals, integrations, and event
  history. Hit `/` to focus from anywhere.
- **Notifications bell**. Personal notifications. Numeric badge
  if any are unread.
- **Profile menu** (top-right). Opens to *Profile, Settings,
  Help & Feedback, Log out*.

---

## Left sidebar (every page)

Same on every page:

- Home
- Operations
- Agents
- Automations
- Reports
- Alerts
- Workflows
- Workspace
- AI Factory
- Topology
- Dashboards
- Managed infra
- Usage & Pricing
- — (separator)
- Settings
- Help & Feedback

Width is fixed; on phones the sidebar becomes a slide-in drawer
behind the hamburger button. Devices and Proposals are tabs
inside *Operations*; Integrations, Clusters, and Onboarding are
tabs inside *Managed infra* — their old standalone URLs still
resolve.

---

## Main column on the Landing page

In order from top to bottom:

### 1. Status pill

A colored pill displaying one of:

- "All systems nominal" (green)
- "X alerts firing" (amber, where X = warning count)
- "Degraded — Y critical" (red, where Y = critical count)

Hover for a breakdown by severity. Click to jump to the *Alerts*
page filtered to the relevant set.

### 2. Greeting

"Good morning, Jane." (Or afternoon / evening based on your time
zone.) Below the greeting, the current local time and a tiny
weather indicator if you've enabled the personal-preferences
weather widget. Otherwise just the date.

### 3. AI Assistant card

A condensed view of the Assistant panel for quick questions
without opening the side panel. Includes:

- A single-line input — type a question, hit Enter, get a
  response inline.
- A "Open full Assistant" link that pops the side panel.
- A list of suggested prompts that change daily ("What's been
  happening since yesterday?", "Anything degraded right now?").

### 4. AI briefing

The generated daily briefing, presented as:

- a **headline summary** line,
- **metric chips** — the day's key figures as big numbers with
  small labels,
- and a grid of **clickable section tiles** (typically *Last 24
  hours*, *Active incidents*, *Recommended actions*, *Capacity
  & cost*), each showing an icon and a one-line preview.
  Clicking a tile expands it in place to reveal the section's
  full text.

Inputs include alerts that fired/resolved overnight, new cloud
resources detected, open change proposals, spend anomalies, and
agent-generated highlights.

Click **"Regenerate"** to rerun with fresh data, or **View full
briefing** to open it as a page (past briefings live under
*Reports → Briefings*).

### 5. Notifications

Personal notifications:

- Mentions in change-proposal threads.
- Alerts assigned to you.
- @-mentions from the Assistant.
- Direct messages from teammates.

Each row has a timestamp, source icon, and a "mark read" check.
The bell badge in the top bar reflects this same feed.

### 6. System health

A single percentage value — "X% of integrations healthy" — with
a sparkline showing the last 7 days. Click to land on *Managed
Infra* filtered to "unhealthy first."

### 7. Quick actions

Four large buttons:

- **Open Assistant** — expands the right panel.
- **View alerts** — jumps to *Alerts*.
- **Approve proposals** — jumps to *Operations → Proposals*
  filtered to "waiting for me."
- **Add integration** — jumps to *Managed Infra → Add*.

### 8. Recent activity (footer)

A timeline of the last 20 significant events in your tenant.
Each row includes the kind, source, and a one-line description.
Click any row to drill into the source object.

---

## Right panel — Assistant

Always docked to the right; default width is ~360 px. Drag the
edge to widen. Components from top to bottom:

- **Header** — "AI Assistant" plus a status dot showing whether
  the underlying LLM is local or cloud.
- **Conversation thread** — your messages and the Assistant's
  replies, scrollable. Tool calls show as expandable cards
  ("queried Prometheus: 4 results"); proposals show as inline
  cards with an "Open proposal" link.
- **Input** — multiline text input with Enter to send and
  Shift+Enter for newline. Markdown is rendered as you type.
- **Footer** — three controls:
  - Tool toggle (allow / disallow specific tools for this
    conversation).
  - Clear conversation.
  - Send feedback (to the Daalu team about an Assistant reply).

---

## Keyboard shortcuts on this page

A few that work from the home page:

- `/` — focus the top-bar search.
- `Cmd-K` / `Ctrl-K` — open/close the Assistant panel.
- `G` then `A` — jump to *Alerts*.
- `G` then `M` — jump to *Managed Infra*.
- `G` then `O` — jump to *Operations*.
- `G` then `S` — jump to *Settings*.
- `?` — show the full shortcuts cheat sheet.

The cheat sheet lists every shortcut available on the current
page.

---

## What you see when there's nothing connected

On a brand-new tenant the page is mostly empty states:

- Pill: "All systems nominal."
- Briefing: "Welcome to Daalu. There isn't anything to report
  yet…"
- Notifications: empty.
- System health: "100% (0 of 0)" — vacuously true.
- Recent activity: empty.

The empty states are deliberately friendly with calls-to-action
("Connect a cloud", "Invite a teammate"). Click through to get
the first signal flowing.

---

## What's next

Chapter 17 covers the *Managed Infra* page — the inventory of
everything Daalu watches for you, and the place you go to add or
remove sources.
