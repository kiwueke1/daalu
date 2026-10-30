# 1. What is Daalu Automation?

Daalu Automation is an operations cockpit for teams who run real
infrastructure. It puts your cloud accounts, your on-prem clusters,
your network devices, and your observability stack into a single
workplace — and pairs them with an AI assistant that can actually do
work on your behalf, not just talk about it.

If you operate things — Kubernetes clusters, switches and routers,
multi-cloud accounts, an alerting pipeline that pages you at 3 AM —
Daalu is built for you.

---

## The problem we're solving

Modern operations teams live inside a tab gauntlet. A typical morning
looks like this: AWS console for one cloud, GCP for another, Datadog
for metrics, Grafana for the on-prem cluster, Slack for the alert
that just fired, PagerDuty for the schedule, Nautobot for the network
inventory, an Atlassian product for the ticket, a wiki for the
runbook, and a terminal with eight `kubectl` contexts switched at the
wrong moments.

Three things go wrong because of this fragmentation:

1. **Investigation is slow.** When something breaks, you don't lack
   data — you lack the ability to *correlate* data across tools fast
   enough. By the time you've stitched together logs, metrics, and
   topology to form a hypothesis, the page has been open for an hour.

2. **Action is even slower.** Even after you know the fix, the
   distance between "I understand it" and "the fix is applied" is
   long: open a PR, get a review, wait for CI, hit the merge button,
   wait for the rollout, monitor it. For a small change that's hours
   of friction; for an off-hours incident, it's an eternity.

3. **AI tools are mostly read-only chatbots.** Today's "AI for ops"
   tools can read your tickets, summarize your alerts, and write
   English about your dashboards. They cannot push config to your
   switch, restart a degraded service, or open a structured change
   proposal that your team can review.

Daalu attacks all three. The platform is **integrated** (all the
sources in one place), it's **action-capable** (the AI can mutate
state, gated by human approval), and it's **opinionated** (it ships
with a way of working — alerts, incidents, change proposals, runbooks
— so you're not rebuilding the workflow yourself).

---

## What Daalu actually does

Concretely, when you log in to Daalu Automation you get:

- **A unified inventory.** Cloud accounts, Kubernetes clusters,
  network devices, observability stacks, and CRMs all show up as
  first-class objects you can query, filter, and drill into.

- **Alerts that explain themselves.** When an alert fires, it
  arrives with a generated explanation, a suggested remediation,
  and a "page on-call" button — not a wall of labels. The
  explanation is grounded in the data Daalu can already see across
  your sources.

- **An AI assistant with hands.** The Daalu Assistant can query
  Prometheus, read pod logs, list cloud resources, propose a fix to
  a network device, or roll back a deployment. Every action that
  changes state is structured as a *change proposal* you approve
  before it executes. Every read is logged.

- **A network source of truth.** Daalu integrates with Nautobot (and
  a small set of similar systems). When the live state of a device
  drifts from the source of truth, Daalu opens a change proposal
  describing the drift — and once approved, it's the same path that
  pushes the fix.

- **Local-first AI.** If you opt in to a local GPU node, every chat
  with the Assistant and every classifier call runs on your hardware
  first, with cloud LLMs as fallback. You pay for the GPU once and
  the AI calls are effectively free.

- **One bill.** Cloud spend, local GPU usage, and Daalu's own subscription
  appear together in the billing page so you can see total cost per
  tenant rather than juggling vendor invoices.

The rest of this book is a tour of those capabilities. Chapter 2
explains the architecture at a high level so the rest makes sense.
Chapters 4–7 take you from "I just signed up" to "my first cloud is
connected and I'm running a synthetic alert end-to-end."

---

## What Daalu is not

To set expectations:

- **Daalu is not a replacement for Datadog or Grafana.** It plugs
  into them — your Grafana dashboards stay where they are. Daalu's
  job is to *correlate* across them, not to be a metrics store.

- **Daalu is not a Slack-bot.** There is a real web application
  with a real UI. Notifications go to Slack and PagerDuty, but the
  workflow lives in Daalu.

- **Daalu is not a no-code platform.** Operators are technical
  people who write YAML and read logs. The product is built for
  someone who already knows what `kubectl get pods` does — the AI is
  a force multiplier, not a substitute for the operator.

- **Daalu is not a configuration management tool like Ansible or
  Salt.** It doesn't try to be the system that *owns* config. It is
  the system that *coordinates and approves* changes, then lets the
  owning system apply them.

---

## How this handbook is organized

The book is laid out as if you were learning Daalu in order from
nothing. **Parts I–II** are required reading for first-time users —
they establish vocabulary and walk you through the first ten minutes.
**Part III** explains the core concepts that every other chapter
assumes. **Part IV** is a feature-by-feature tour you can read
linearly or skip into. **Parts V, VI, and VIII** are reference: integrations,
advanced workflows, and troubleshooting.

A glossary lives at the end. If a term is bolded the first time it
appears (like **tenant** or **change proposal**), it's defined in
the glossary too.
