# 19. Agents

The Daalu **Agents** page is where you see the autonomous helpers
running in your tenant. Each agent is a small program with a
schedule (or a trigger) and a focused job: generate a daily
briefing, sweep for expiring certs, propose tuning changes,
investigate a recurring alert.

URL: `https://ops.example.com/agents`.

This is one of the most powerful pages once you've been using
Daalu for a few weeks. New customers often don't notice it
exists. By month two, it's where they live.

---

## What an agent is

An agent is:

- A named, configured behaviour.
- Bound to a schedule (cron) or a trigger (event match).
- Implemented as a sequence of Assistant-like tool calls,
  ending in a write to one of: the timeline, the proposal
  queue, or a notification channel.
- Subject to the same approval rules as the Assistant. If the
  agent's plan ends in a write, it routes through a change
  proposal that a human approves.

Agents are tenant-scoped. Each tenant has its own set, can
enable/disable each, and can authoring custom ones (Chapter 21
on workflows).

---

## The shipped agents

Out of the box, every tenant gets these:

### Briefing agent

Runs once a day at your tenant's briefing time. Generates the
home-page briefing. You met this one in Chapter 5.

### Reconciler agent (SoT)

Runs every 5 minutes per device (configurable). Detects drift
and writes proposals. Met in Chapter 13.

### Alert-explainer agent

Runs ~30 seconds after each new alert. Queries the relevant
metrics and logs, writes a 2–3 paragraph explanation that
appears on the alert detail page. The first time you see this
in action is usually a memorable moment.

### Cluster-tunnel-health agent

Runs every minute. Verifies that each federated cluster's
WireGuard tunnel is healthy. Writes a `cluster.tunnel.down`
event if a handshake is older than 5 minutes; promotes to
critical alert at 15 minutes.

### Cost-anomaly agent

Runs every hour. Compares today's cloud spend (so far) against
your trailing 7-day average; flags deviations larger than the
threshold in *Settings → Briefings → Cost anomaly*.

### Cert-expiry agent

Runs daily. Sweeps every connected source's TLS certificates
and writes a notification if any is within 30 days of expiry.

### Demo-data seeder (only in `staging` tenants)

Doesn't run in production tenants. Used to populate demo data
for evaluation environments.

---

## The Agents page layout

The page is a table:

| Agent | Status | Last run | Next run | Success rate (7d) | p50 latency | ⋯ |

A row per shipped or custom agent. Click any row for the
**agent detail page**:

- **Configuration** — schedule, trigger, parameters (e.g.,
  briefing time, anomaly threshold).
- **Run history** — last 100 runs with timestamps, status,
  duration, output artifact (e.g., the briefing it generated).
- **Logs** — structured logs from the agent's last run.
- **Test now** — trigger an immediate run.
- **Disable** — pause this agent (admin only).

---

## Enabling and disabling

By default, all shipped agents are enabled in every tenant
except `cert-expiry` (off by default; some tenants find it
noisy).

Toggling an agent off:

1. Click the row.
2. Toggle **Enabled** off in the configuration panel.
3. The next scheduled run is skipped; on-demand triggering
   from this page still works.

Disabling vs deleting: shipped agents can't be deleted, only
disabled. Custom agents (workflows) can be deleted.

---

## Per-agent settings

Each agent has its own configuration UI:

### Briefing agent

- Time of day (your tenant's time zone).
- Days of the week.
- Inputs (which sources to include).
- Output channels (in-app, Slack, email digest).

### Reconciler agent

- Per-device interval (default 5 min).
- Concurrency limit (how many devices may be checked in
  parallel — defaults are conservative for low-end Nautobot
  instances).
- Diff sensitivity (ignore comments, ignore whitespace, etc.).

### Alert-explainer agent

- Maximum tokens spent per explanation.
- Sources to query (Prometheus, Loki, all, none).
- Model preference (cheap vs. high-quality).

### Cost-anomaly agent

- Per-cloud thresholds.
- Tenant-wide threshold.
- Notification channel.

Each agent's configuration is also editable via the API for
infra-as-code workflows.

---

## Authoring custom agents

To author your own:

1. Go to *Automations → New automation*.
2. Pick a trigger (cron schedule or event match).
3. Build the plan: a sequence of tool calls and conditions.
4. Save. Daalu adds it as a new row on the Agents page.

Chapter 21 covers the workflow authoring UI in detail. The
agents you create here appear with type "Custom" on the agents
page and have the same run-history features.

---

## When an agent fails

Agents fail like any other program. The most common modes:

- **Tool error.** A downstream system returned an error (cloud
  API throttled, Loki unreachable). The run is marked failed
  with the error in the logs. The agent retries on its next
  scheduled run.
- **Approval missing.** An agent's plan ended in a proposal
  that's been waiting for approval for >24 hours. The agent
  pings notification channels to remind you.
- **Timeout.** Long-running queries can hit the per-agent
  timeout (default: 5 minutes). Either raise the timeout or
  break the agent's work into smaller chunks.

The Agents page's "Success rate (7d)" column is the best place
to spot trending issues. A green 100 % means the agent is happy.
A 70 % means you should investigate.

---

## What's next

Chapter 20 covers *Alerts*. The Assistant and several agents
interact with alerts heavily; the page itself has its own UX
worth touring.
