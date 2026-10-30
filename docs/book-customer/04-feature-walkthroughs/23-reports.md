# 23. Reports

The *Reports* page is where the agent's view of your environment
gets written down. It has two tabs:

- **Briefings** — AI-generated operational summaries (the default
  view). One per channel per period.
- **Query** — ad-hoc and saved queries over Daalu's data model
  (events, alerts, devices, proposals, integrations).

URL: `https://ops.example.com/reports`.

---

## The Briefings tab

This is what the page opens to. The header reads *"AI-generated
operational intelligence — interactive, not static."* and a
**Regenerate** button sits at the top right.

Beneath the header is a list of briefings, newest first. Each row
shows:

- The **channel** (e.g., `infra`).
- The **date**.
- A short title (e.g., *Infra briefing — 2026-05-24*).
- A summary paragraph.
- Structured sub-sections: *Last 24h*, *Active incidents*,
  *Recommended actions*, *Capacity & cost notes*.

Click a row to open the full briefing on its own page, where you
can:

- Drill into any cited event, alert, or incident — the briefing
  links back to the underlying records.
- Ask the Assistant a follow-up scoped to that briefing
  (*"explain the capacity warning"*).
- Share a permalink with a teammate.

Older briefings live under an **Earlier** section at the bottom
of the list.

### Why briefings exist

A briefing is what the agent would say if a human asked *"what
happened since I last looked?"* — written once per channel per
period, so the answer is ready before the question. The agent
draws on every event, alert, incident, and proposal in the
window. On a quiet day it'll say so honestly; on a busy day it'll
lead with what needs attention.

Briefing channels are configured under *Settings → Briefings*.
The default schedule is weekday 07:00 local; you can change cron
and delivery (Slack/email) per channel.

### Regenerate

The **Regenerate** button asks the agent to write a fresh
briefing now, against current data — useful when something
just happened and you don't want to wait for the next scheduled
run. Regenerated briefings replace the latest entry for the day;
older days are not touched.

---

## The Query tab

The Query tab is for asking *"show me the rows"*. There are two
ways to write a query — pick whichever fits the moment.

### Structured query

A three-section builder:

1. **From** — pick the entity: *events*, *alerts*, *incidents*,
   *devices*, *integrations*, *change proposals*.
2. **Where** — filters on that entity's fields.
3. **Display** — table columns, or aggregation (count, sum,
   group-by).

Click **Run** to see results. Click **Save** to name the query
and keep it in the Reports list.

### Natural-language query

Type the question in plain English. The Assistant translates it
into the underlying query, runs it, and renders the result. You
see both:

- The **answer** (table or chart).
- The **structured query** the Assistant built, in case you want
  to tweak it and re-run, or save it as a structured query.

Examples that work:

- *"events from nautobot/main in the last hour"*
- *"top 10 noisiest alert rules this week"*
- *"devices that haven't reconciled in 24h, grouped by site"*
- *"all approved change proposals last month, CSV please"*

If the Assistant isn't sure what you meant, it asks one
clarifying question rather than guessing.

### Saving and scheduling

Either query type can be saved with a name. Saved queries appear
in a list on the Query tab and can be:

- **Pinned** to a dashboard tile.
- **Scheduled** — runs on a cron and delivers to Slack or email.
  Format options: Markdown table, CSV, PNG of the chart.
- **Exported** — CSV / JSON / PDF, respecting the current filter.

---

## Dashboards

A dashboard is a grid of tiles, each backed by a saved query.
Tile types: table, single-number stat card, line chart, bar
chart, pie chart. Drag to resize, save the layout. Multiple
dashboards per tenant is fine.

A dashboard tile can be pinned to the Home page — useful for a
team-wide view (e.g. *"daily netops standup"*) everyone sees on
login.

---

## Examples to start from

The Query tab ships with examples you can clone:

- **Pending change proposals waiting >24h** — flags neglected
  approvals.
- **Top alerts by volume this week** — for tuning noisy alerts.
- **Cloud resources without owner tags** — for cost attribution.
- **Devices not reconciled in 24h** — for catching reconciler
  gaps.
- **Users who haven't logged in for 30 days** — for offboarding
  audits.

Each example has a **Clone and modify** button.

---

## Permissions

By default, all users can view briefings, run queries, and save
their own. Admins can additionally:

- **Pin** a saved query or dashboard to the Home page.
- **Delete** queries authored by another user.
- **Edit briefing schedules** under *Settings → Briefings*.

---

## What's next

Chapter 24 covers the *Settings* page — profile, tenant, and
preferences.
