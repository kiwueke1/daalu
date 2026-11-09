# 2. How it works

This chapter is the mental model you need to make sense of every
other chapter. We'll go from a single picture down to the moving
parts, then trace what happens when you connect a cloud account or
ask the Assistant a question.

You won't see any code. You will see component names, since you'll
encounter them in the UI and in support conversations.

---

## The 30-second picture

Daalu Automation is split into three pieces:

```
                ┌───────────────────────────────────────────┐
                │            Daalu cloud (SaaS)             │
                │                                           │
                │  operator app  ←  observability  →  AI    │
                │       ↑               ↑              ↑    │
                │       │ data flows    │              │    │
                │       │ in & out      │              │    │
                └──────-│───────────────│──────────────│────┘
                        │               │              │
                        │               │              │
                  ┌─────┴─────┐    ┌────┴────┐    ┌────┴────┐
                  │ your cloud│    │ your    │    │ optional│
                  │ accounts  │    │ k8s/    │    │ local   │
                  │ (AWS/GCP/ │    │ on-prem │    │ GPU node│
                  │  Azure)   │    │ cluster │    │ (vLLM)  │
                  └───────────┘    └─────────┘    └─────────┘
```

1. **The Daalu cloud** is a multi-tenant SaaS. It runs the operator
   app you log in to (the website), the supporting backend services,
   and the AI infrastructure.

2. **Your sources** are the things you operate. Cloud accounts (AWS,
   GCP, Azure), Kubernetes clusters (yours), network devices, your
   observability stack (Prometheus / Grafana / Loki / Thanos),
   ticketing (PagerDuty), etc. Daalu connects to them in one of two
   ways:

   - **Direct integration** — for SaaS systems with APIs (Slack,
     AWS, Nautobot Cloud, Grafana Cloud), Daalu calls their public
     API using credentials you provide.
   - **Daalu Edge** — for things behind your firewall (an on-prem
     Kubernetes cluster, a GPU node, a network device with no
     public endpoint), you deploy a small edge agent inside your
     network that opens an outbound WireGuard tunnel back to the
     Daalu cloud. Nothing on your side needs to accept inbound
     traffic.

3. **Optional local AI** — if you want chatbot calls and classifier
   inference to run on your own hardware, you deploy a small vLLM
   pod on a GPU node (yours). The Daalu cloud routes inference to
   that node when it's online and falls back to commercial LLMs
   when it's not.

That's the whole picture. The rest of this chapter zooms into each
piece.

---

## The operator app — what you see in your browser

The operator app at `https://ops.example.com` is the surface you spend
your time on. It is a single web application organized into pages
along the left sidebar, grouped by what you came to do:

- **Home** — the landing page; a daily briefing of what's changed,
  what's degraded, and what wants your attention.

**Respond**

- **Alerts** — the alert inbox; each alert has a generated
  explanation, suggested actions, and the ability to escalate.
- **Investigations** — every alert the AI investigated, where you
  grade whether it found the right root cause.
- **Workflows** — multi-step automations, with run history and
  the runs waiting for your approval.

**Automate**

- **Automations** — the automations you've authored, with live
  run status.
- **Agents** — autonomous helpers that run on schedules or on
  triggers (e.g. "every weekday at 8 AM, generate a briefing for
  the platform team").
- **Runbooks** — your team's fixes, written down where the AI can
  use them.
- **Autonomy policy** — what the AI may change on its own, what
  waits for approval, and what it may never do.

**Infrastructure**

- **Managed infra** — everything Daalu watches or runs for you, as
  tabs: Kubernetes clusters & observability, the network/server
  management environments (each with a live map of its network
  fabric), guided onboarding, and integrations (cloud accounts,
  monitoring stacks, notification channels, sources of truth).
- **AI Factory** — observability and operations for the GPUs
  serving your AI.
- **Operations** — devices, drift, change proposals; this is the
  network operator's home page.
- **Workspace** — the browser coding assistant: VS Code in a tab
  with an AI agent constrained to your own inference.

**Insights**

- **Dashboards** — your Grafana dashboards, rendered natively from
  your own metric and log stores.
- **Reports** — AI-generated operational briefings plus a query
  tab for ad-hoc and saved analyses over events, alerts, devices,
  and proposals.
- **Maturity**, **Evals** and **Audit log** — how far your team has
  climbed the AI operations maturity model, how well the AI
  performs on test scenarios, and a tamper-evident record of every
  action.

At the bottom of the sidebar:

- **Usage & Pricing** — your plan, AI usage, and how much ran on
  your own GPU.
- **Settings** — your profile, your team, API tokens, notification
  preferences.
- **Help & Feedback** — version info and a way to write to the
  team that builds the product.

The Assistant is *not* a separate page — it lives in the right rail
that follows you across the whole app (on a phone, behind the
**Assistant** button). From any page you can open it, ask a question
grounded in what you're looking at, and continue.

Part IV of this book is a chapter-per-page tour.

---

## Tenants — your slice of the cloud

Every customer of Daalu Automation operates inside a **tenant**.
A tenant is your private workspace: your users, your integrations,
your alerts, your spend.

Important properties of tenants:

- **Tenants are isolated.** Users in your tenant never see another
  tenant's data, and the API enforces this at every endpoint.
- **A user belongs to exactly one tenant.** Switching tenants means
  logging in to a different account.
- **Quotas are per-tenant.** Cloud-LLM tokens, alert volume,
  retention windows — they all meter against your tenant.
- **Billing is per-tenant.** One tenant gets one invoice.

The first user to sign up creates the tenant. They're the
**admin**. Admins can invite other users, change integrations, and
mint API tokens. Everyone else is a regular user, which is enough
to do investigation and approve change proposals, but not enough to
rotate credentials.

---

## Integrations — how Daalu sees your world

An **integration** is Daalu's connection to one of your external
systems. There are dozens of them shipped with the product; you'll
find the full list under *Managed infra* and a catalog in Part V.

There are three categories of integration, each with a different
authentication model:

1. **API-key integrations** (Slack, PagerDuty, Nautobot, Grafana
   Cloud). You generate a token in their system, paste it into
   Daalu, and Daalu uses it to call their API.

2. **Cloud-account integrations** (AWS, GCP, Azure). You create a
   role/service-account in your cloud and grant Daalu the
   permissions it needs — usually read-only at first. You can grant
   write later if you want the Assistant to be able to act.

3. **Cluster integrations** (your own Kubernetes clusters). You
   deploy the **Daalu Edge** chart into your cluster, which opens an
   outbound WireGuard tunnel. Once the handshake completes, your
   cluster appears as **connected** in *Managed infra*. From that
   point, the Daalu cloud can call read-only and write APIs in your
   cluster, with every write logged.

Integrations are the substrate that everything else relies on. The
Assistant can only see what an integration exposes. Alerts can only
fire on data that came in through an integration. The chapters in
Part V have step-by-step setup guides for each one.

---

## Events, alerts, and incidents — the operational currency

Every observation Daalu makes is an **event**. An event is a small
JSON object with a source, a kind, a severity, and a payload. A
single CPU-pressure metric from Prometheus is an event. A failed
build in GitHub Actions is an event. A new device discovered by the
network reconciler is an event.

Events flow into Daalu and become one of three things:

- **Just an event** — recorded in the timeline, available for
  search and queries, but not actively pushed at you.
- **An alert** — when an event matches an alerting rule, it's
  promoted to an alert. Alerts have a status (firing/resolved), a
  severity (info/warning/critical), and a runbook. They show up on
  the *Alerts* page and trigger notifications.
- **An incident** — when one or more alerts are grouped (manually
  or by an agent) into a single ongoing issue, it's an incident.
  Incidents have an assignee, a timeline, and a postmortem.

The grouping decision matters because it controls whether your
phone rings. A flapping disk-pressure metric should generate
hundreds of events but exactly one alert and zero incidents.
Chapter 20 covers the rules.

---

## The AI Assistant — the action layer

The Assistant is what makes Daalu different from being "another
observability dashboard." It can do three classes of thing:

- **Investigate** — query Prometheus, read pod logs, list AWS
  resources, walk a Nautobot device tree, summarize an alert. All
  read-only.
- **Propose** — write a change proposal that, if approved by a
  human, mutates state somewhere. "Drain node X and reschedule its
  pods." "Update the ACL on switch Y to allow tenant Z." "Roll
  back the latest deploy of the api service."
- **Notify** — page a person, post to Slack, or escalate to
  PagerDuty.

Investigate is unrestricted. Propose is always staged through a
human approval, except when an admin has pre-approved an automation
for that tenant. Notify obeys your notification preferences.

The Assistant lives in the right rail of most pages (behind the
**Assistant** button on a phone). Asking a question on the *Alerts* page passes the current
alert context automatically — you don't need to copy IDs around. On
*Operations* it picks up the device you're viewing. This is one of
the small things that compounds across a working day.

---

## Cluster federation — getting Daalu close to your stuff

A lot of operators have things behind a firewall. The standard
answer in this industry — "open a port, give us a hostname" — is
a non-starter for most production networks. Daalu uses a different
pattern: **outbound-only WireGuard tunnels**.

Here is how it works, in plain English:

1. You go to *Managed infra → Clusters & observability → Onboard
   cluster* and request an invite. The Daalu cloud generates a one-shot bootstrap token,
   valid for one hour.
2. You install the **Daalu Edge** Helm chart in your cluster with
   that token. The chart contains a WireGuard agent and a small
   bootstrap service.
3. On startup, the edge agent calls back to the Daalu cloud over
   HTTPS, presents the bootstrap token, and exchanges it for a
   long-lived WireGuard configuration. The token is then burned.
4. The agent brings up the WireGuard tunnel. From the Daalu cloud's
   side, your cluster is now an additional endpoint on a private
   network. The cloud can reach your services; nothing outside the
   tunnel can.
5. The cluster row in *Managed infra* turns **connected** within
   ~30 seconds. From there, all the Daalu features that need
   cluster access (the Assistant's kube tools, the GPU router, the
   local-LLM scrape) just work.

This pattern means you never open an inbound port and never give
Daalu credentials to a long-lived API key. Revocation is
instantaneous — delete the edge chart, the tunnel dies.

Part VI Chapter 34 walks through the full deploy. Chapter 14 covers
the day-to-day operator-facing view.

---

## Local GPU and the LLM router

Some customers run their own GPUs and would prefer that AI
inference happen locally. Daalu supports this directly. The flow:

1. You buy a GPU node and deploy a small **vLLM** pod on it. The
   pod hosts an open-weight model (Llama or Qwen) and serves an
   OpenAI-compatible HTTP API on a port that only Daalu can reach
   through the federation tunnel.
2. In the operator app, the GPU appears as **online** on the
   *Usage & Pricing* page once the router has successfully called it.
3. From then on, every chatbot turn, every classifier call, and
   every routine inference task tries the local GPU first.

The router uses simple rules: if the local pod is online and the
model can serve the request, use it; otherwise fall back to a
commercial LLM. The result is that 80–95 % of inference calls (by
volume) run on your hardware, costing you nothing per call. Heavy
or specialty requests still go to a cloud LLM. Chapters 15 and 33
explain the routing decisions in detail.

---

## Where data lives

On the **Standard** tier (the default), the rows you create in
Daalu — alerts, change proposals, runbook notes, the Assistant's
conversation history — are stored in Daalu's managed Postgres
database, isolated per tenant. Logs and metrics collected through
integrations live in Daalu's observability stack (Prometheus + Loki)
with the retention window defined by your plan.

What does **not** leave your environment, regardless of tier:

- Your secrets (API keys, kubeconfigs). They're stored encrypted
  at rest and only used in-process when needed.
- Your network device configs. Daalu reads them on demand and does
  not snapshot them by default.
- Your cloud-account contents (S3 buckets, BigQuery rows, etc.).
  Daalu calls the cloud APIs as needed; it doesn't copy your data.

If your contract requires that **even the alerts / proposals /
conversation history never live on Daalu's stack**, there is a
**Daalu Private (full)** tier where all of those rows are stored
in a Postgres on your own cluster. The Daalu hub becomes a pure
control plane: it serves the UI shell, holds your account row,
and proxies your browser's API calls through the WireGuard tunnel
to a tenant-scoped backend running inside your cluster. Setup is
a single Helm command. Chapter 27 has the details.

Chapter 57 of the engineer book has the formal tenant-isolation
guarantee for anyone who needs to brief a security team.

---

That's the model. The rest of the book will keep referring back to
these names: tenant, integration, event, alert, change proposal,
edge, router, assistant. If you ever feel lost on a page, come back
to this chapter.
