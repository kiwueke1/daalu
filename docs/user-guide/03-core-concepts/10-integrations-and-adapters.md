# 10. Integrations and adapters

Everything Daalu shows you about your environment comes through
an **integration**. Cloud accounts, observability stacks, CRMs,
network sources of truth — they all flow in the same way. This
chapter explains the concept so the integration-specific chapters
in Part V can be skimmed instead of read in full.

---

## The shape of an integration

In Daalu, an **integration** is a configured connection to one of
your external systems. Each integration has:

- A **type**, telling Daalu what kind of system it is
  (`aws`, `gcp`, `prometheus`, `slack`, `nautobot`, …).
- A **name**, chosen by you ("Acme prod AWS," "Eng-team Slack").
- A **status** — connected, disconnected, error.
- A **credential**, stored encrypted in Daalu's database. The
  shape varies by type — a Role ARN for AWS, a webhook URL for
  Slack, a bearer token for Nautobot.
- A **scope**, optionally narrowing what Daalu can see (e.g., a
  specific AWS region or a specific Slack channel).

Integrations live on the **Managed infra** page. We'll tour that
page in Chapter 17; for now, you should know that *every* source
of data in Daalu is one of these rows.

---

## Adapters: what actually runs

Behind every integration is an **adapter** — a piece of code in
Daalu that knows how to talk to one specific external system.
When the integration is configured, the adapter:

- **Probes** the system on a schedule (typically every 1–5
  minutes for status checks, longer for full inventory).
- **Translates** what it sees into Daalu's internal data model —
  events, alerts, devices, recommendations.
- **Acts** on the system when the Assistant or a change proposal
  needs to mutate state.

You don't interact with adapters directly. They're invisible
infrastructure. But the term shows up in support conversations
("the AWS adapter hasn't seen a successful probe in 10 minutes")
and in the engineer book, so we wanted to define it once.

---

## Categories of integration

There are roughly five kinds, each with a different
authentication and usage model.

### Cloud accounts

AWS, GCP, Azure (Chapter 28). Read-only: the AI uses them to
describe instances and read logs and metrics while it
investigates, and the Assistant can answer questions about your
resources. On AWS, firing CloudWatch alarms also arrive as alerts.

### Observability stacks

Prometheus, Grafana, Loki, Thanos, OpenSearch (Chapter 31). The
adapter exposes the system to the Assistant as a query target —
it can run PromQL, LogQL, OpenSearch DSL on your behalf. Alert
rules defined in your observability stack flow into Daalu and
show up on the *Alerts* page.

### Notification channels

Slack, PagerDuty, email (Chapter 29). The adapter sends
outbound notifications. Daalu doesn't poll these systems; they're
write-only from Daalu's perspective.

### Source-of-truth systems

Nautobot today (Chapter 30); Netbox was on the original roadmap
but is currently deferred. The adapter pulls device inventory,
runs reconciliation, and writes back changes. This is the only
category where Daalu's writes can change physical-network state.

### Webhooks

A general-purpose "receive an arbitrary event" integration
(Chapter 33). Useful when none of the others fit — a CI pipeline
that wants to declare a deploy is happening, a homegrown alerter
that wants to feed Daalu's alert inbox.

---

## How adapters talk to the rest of Daalu

The same vocabulary keeps coming up: events, alerts, inventory,
proposals. Adapters are how those things get created.

- An adapter that finds something interesting on the external
  system writes an **event**. This is the lowest-level
  representation — a timestamped JSON blob with a source and a
  kind.
- An event matched against a rule becomes an **alert**. Alerts
  are what page humans.
- An adapter that crawls structured data (cloud resources,
  network devices) writes **inventory** rows. Those rows back
  the various list views in the UI.
- An adapter that detects a discrepancy between source-of-truth
  and live state writes a **change proposal**, which routes to
  humans for approval before any write happens.

The point of this section: when you see an event, an alert, or a
proposal in the UI, you can always trace it back to a specific
integration. The "Source" column on each list view tells you
which one.

---

## Healthy integrations vs broken integrations

Each integration has a health check that runs every minute. The
row on *Managed infra* shows the result as a status badge:

- **Connected** (green) — the adapter successfully called the
  external system.
- **Configured** (amber) — saved, but not reporting yet. Usual for
  the first minute after you connect it.
- **Error** (red) — the adapter could not call the system.
  Credentials may have expired, the URL is wrong, the system is
  down.

To see why a row is red, click **Edit** on it and then **Test
connection** — the result shows the system's own error message.
For an ingest source on the **Integrations** tab, **Check now**
pulls new alerts from it immediately instead of waiting for the
next scheduled pass.

A few common reasons an integration goes red:

- **Credential expiry.** Most cloud roles, OAuth tokens, and
  API keys expire on a schedule you set. Set a calendar reminder
  to rotate before they do.
- **Permission changes.** Your cloud team removed a permission
  from the role Daalu uses. The adapter sees an
  AuthorizationDenied response and goes red.
- **Network reachability.** For integrations that live inside
  your network and reach Daalu over the federation tunnel, the
  tunnel going down (e.g., the edge pod crashed) takes the
  integration down with it.

Each integration chapter (Part V) has a "troubleshoot" section
specific to its failure modes.

---

## Why the model is this shape

Two design choices that often come up in customer questions:

### Why isn't an integration just a credential?

Because it also carries *where* and *how* Daalu reaches the
system — a URL, a region, whether to go through your cluster
tunnel — and its live health. Each integration is a row you can
test, edit and disconnect on its own, and every change to it is
audited.

### Why are observability stacks "integrations" and not "the metrics store"?

We deliberately don't try to replace your Prometheus or your
Grafana. They live where they live; Daalu connects to them. The
upside is that you don't have to migrate metrics history or
rebuild dashboards to start using Daalu. The downside is that
Daalu's view of metrics is whatever your observability stack
sees — if Prometheus is down, Daalu doesn't have its own
fallback.

We've found this tradeoff is right for the audience: operators
who already have an observability stack they're not going to
abandon.

---

## What's next

Chapter 11 explains how events become alerts and how alerts
become incidents — the operational currency of Daalu. Chapter 12
covers the AI Assistant in detail. The remaining Part III
chapters each focus on one specific class of integration.
