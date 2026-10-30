# 22. Billing and spend

The *Billing* page is one of the most-loved pages in Daalu. It
puts cloud spend, local GPU usage, commercial LLM spend, and
your Daalu subscription on a single screen, so the answer to
"what does running this stuff cost?" is one click away.

URL: `https://ops.example.com/billing`. Admin-only.

---

## Layout

Top of page: a summary band with three numbers.

- **This month so far** — total spend across all connected
  clouds.
- **Last month** — total at the same date in the prior month
  (for comparison).
- **Projected month-end** — straight-line extrapolation; not
  to be relied on for tight budgets, but useful for trend
  spotting.

Below, four sections:

1. **Local GPU + LLM router**
2. **Cloud spend**
3. **Daalu subscription**
4. **Spend breakdown and forecast**

We'll cover each.

---

## Local GPU + LLM router

A panel showing the status and value of local inference.

### Status pill

- **Online** — local GPU has responded to a router probe in the
  last 5 minutes.
- **Deploying…** — Daalu is rolling the serving stack out onto
  your cluster; the collapsible **Deploy progress** panel below
  the pill shows the rollout live (pod state, blocking reasons,
  cluster events, and the serving logs once they exist).
- **Offline** — last response is older. Probably means the
  serving pod is unhealthy or the federation tunnel is down —
  the same Deploy progress panel is offered here too, because
  its events and logs are how you find out why.
- **Not configured** — no local GPU set up. The router is
  using its fallback tiers for everything. This is where the
  **Connect your GPU** button lives — chapter 35 walks through
  it.

The pill and the AI Factory page share one health probe, so
they always agree.

### SKU picker

Four cards showing the commercial equivalent of each tier of
LLM call:

- **Classifier (small)** — short prompts, low context.
- **Chat (default)** — typical assistant turns.
- **Chat (large context)** — long-context summarization.
- **Embedding** — vector embeddings.

Each card has the commercial $/1M tokens price and the
estimated "what you saved this month" figure.

### Inference trend

A stacked bar chart of inference calls per day for the last 7
days, broken down by destination (local vs cloud) and tier
(classifier / chat / embedding).

The first time you see this on a tenant with local inference
turned on, the proportion of green (local) to gray (cloud) is
usually a happy moment.

### Spend

Total LLM spend this month, with the comparison "would have
been $X without local inference."

---

## Cloud spend

A row per connected cloud account showing:

- Cloud name.
- This month's spend so far.
- Last month's spend at the same date.
- Trend arrow.

Click a row to drill into:

- Spend over time (last 90 days).
- Top services by spend.
- Top regions by spend.
- Top tags by spend (if you tag religiously).
- Largest individual resources (top-10 EC2 instances, top-10
  S3 buckets by storage, etc.).

The data comes from the cloud's billing API. Latency from
"resource exists" to "shows up here" is typically 24 hours;
spend numbers stabilize fully 48–72 hours after the month
closes.

---

## Daalu subscription

What you're paying us. Includes:

- Plan name (Starter / Team / Business / Enterprise).
- Seat count (active users).
- Add-ons (local inference, extended retention, etc.).
- Current month-to-date charge.
- Next invoice date.

**Download invoice** → PDF of the last issued invoice.

**Manage plan** → Stripe-hosted portal where you change card,
update billing email, change plan tier.

If you're on an Enterprise contract, the panel shows your
contracted spend and any overage; invoicing happens out of
band.

---

## Spend breakdown and forecast

A combined view spanning cloud + Daalu + LLM:

- **By month** — bar chart of total spend per month for the
  last 12.
- **By category** — pie chart of "compute, storage, transfer,
  managed services, Daalu, LLM" for the current month.
- **By source** — same as cloud spend section but unified.

The **forecast** is a straight-line extrapolation of the
current month-to-date. It's a rough number; for tighter
forecasts, hook your accounting team's tools into the
exported CSV (next section).

---

## Exporting

Top-right of the page: **Export → CSV / JSON.**

The CSV is one row per (cloud-account, service, tag-set, day).
Importable into the spreadsheet of your choice.

The JSON has the same data with nested structure preserved.
Useful for piping into a warehouse via a workflow.

Both exports respect the current filter selection (date range,
selected accounts). Exporting the whole tenant's history is
also possible from *Settings → Tenant → Export billing.*

---

## Alerts on spend

Cost anomalies fire as alerts (see the cost-anomaly agent in
Chapter 19). You can tune the thresholds at *Settings →
Briefings → Cost anomaly*:

- Daily threshold: % deviation from trailing 7-day average.
- Service threshold: per-service spike threshold.
- Notification channel: who gets paged.

Default thresholds catch obvious "your bill is 3× yesterday's"
spikes; tune them tighter once you've seen a few false
positives.

---

## What's *not* on this page

A few things on purpose:

- **Detailed cost allocation across teams.** Cost-allocation
  requires consistent tagging across your clouds, which most
  teams don't have. The page shows tag-based breakdowns when
  available but doesn't try to be a Cloudability replacement.
- **Cost optimization recommendations.** Things like
  "rightsize this EC2 instance" are surfaced via the
  Assistant and the recommendations agent, not on this page.
- **Per-user usage.** Daalu doesn't bill per-action, so
  per-user cost is rarely meaningful. The plan is per-seat.

---

## What's next

Chapter 23 covers the *Reports* page — AI-generated briefings
plus a query tab for ad-hoc and saved analyses.
