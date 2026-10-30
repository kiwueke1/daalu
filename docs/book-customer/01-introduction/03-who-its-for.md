# 3. Who Daalu is for

Daalu Automation is opinionated. It's designed for a specific kind
of team doing a specific kind of work, and you'll get the most out
of it if you recognize yourself in this chapter. If you don't,
that's useful too — it'll save you the trial.

---

## The teams Daalu was built for

### Platform and SRE teams

You run a Kubernetes-shaped platform, you're on call, and you spend
a meaningful slice of your week investigating things. You already
have Prometheus, Grafana, an alerting tool, and a way to deploy
code. What you don't have is one place to look when the page fires
at 2 AM, and you don't have a co-pilot that can do anything besides
explain what you can already see.

Daalu was built around your workflow. The *Alerts* page is the
first thing a platform person sees on Monday morning. The Assistant
on the *Operations* tab can read pod logs, query metrics, and
suggest a restart — and if you tell it to apply the restart, it
opens a change proposal that another engineer can approve in two
clicks.

### Network operators (NetOps)

You manage routers, switches, firewalls. You use Nautobot, Netbox,
or one of the home-grown databases that does the same job. Your
biggest operational problem isn't building config — it's keeping
the source of truth aligned with what's actually on the boxes, and
auditing every change.

Daalu's Source of Truth (SoT) integration ingests your Nautobot
inventory, reconciles it against live device state on a schedule,
and surfaces drift as structured change proposals. The same path
that flags the drift is the path that fixes it: you read the
proposal, you approve it, the executor pushes config to the device.
Each step is logged, each step is reversible.

### Multi-cloud platform teams

You have AWS for production, GCP because someone won a contract,
Azure because someone else's enterprise SSO is hosted there, and
on-prem for the bare-metal workloads. The world's worst spreadsheet
lists which accounts exist and who pays for them.

Daalu inventories all of it. *Managed Infra* shows every connected
account, what's healthy, what's been touched recently, and what
it's costing. The Assistant can answer "how many EC2 instances do
we have in us-east-1 versus eu-west-1 right now?" without you
switching tabs.

### Small operations teams at growing companies

You're a team of three or six, you wear five hats each, and you're
the operations function inside a product company that doesn't yet
have a dedicated platform engineer. Datadog is too expensive at
your scale. PagerDuty is too narrow. Spending a week building a
custom dashboard out of Grafana feels like it's not your job.

Daalu is open source and self-hostable, and the product
deliberately bundles the workflow you'd otherwise stitch together
from five separate vendors.

### Internal tooling and developer-experience teams

You build the tools your engineers use. Daalu's API and personal
access tokens make it embeddable: you can script against your
inventory, post events from CI, or have your own internal bot defer
to the Daalu Assistant for ops questions.

---

## Roles inside a customer

Once a team is using Daalu, individuals tend to fall into one of
three patterns:

### The admin

The admin manages the tenant. They invited everyone in, they own
the integrations, they configure who gets paged for what. There
is usually one or two of them per team. They read the *Billing*
page on Monday and the *Settings* page when someone leaves.

### The on-call operator

The bulk of users. They use Daalu reactively — when an alert fires
or a customer complains. They live on the *Alerts* page and the
*Agents* page. They use the Assistant heavily to investigate. They
approve change proposals other people open.

### The automation author

Some people in every team eventually become the person who writes
workflows and configures the agents. They live in the *Workflows*
page, the *Operations* tab, and the *Settings → Personal access
tokens* page. They turn manual processes into automatable ones.

These aren't formal roles in the access-control system — the only
formal distinction is admin vs. non-admin. They're just patterns
of usage.

---

## When Daalu is not the right tool

A few honest disqualifiers.

### You don't operate infrastructure

If your job is to write product code and ship it, and someone else
runs the cluster, you don't need Daalu. You need your existing
deploy pipeline plus whatever observability your platform team
already runs. Daalu is for the people *behind* you.

### You operate exactly one cloud and one cluster

Daalu's strength is correlation across many sources. If you have a
single AWS account, no on-prem, no devices, and your team is happy
with the AWS console, Daalu's integration overhead is more friction
than it's worth.

### Your network is air-gapped

The product assumes the operator app at `ops.example.com` is
reachable. If your security model forbids any browser session from
seeing infrastructure metadata, the SaaS form of Daalu won't fit.
Self-hosted Daalu is on the roadmap; talk to us.

### You want a fully autonomous AI operator

Daalu's AI is action-capable but it's not autonomous. Every state
change goes through a human approval by default. If you want a
system that decides on its own when to scale your cluster, restart
a service, or page a person, configure those rules in your existing
controllers; Daalu's role is to coordinate, not to *be* the
controller.

### You need certified compliance regimes today

If your contract says you require PCI-DSS, HIPAA, FedRAMP, or
ISO 27001 attestations *today*, ask before you sign. The platform
is built to support these — the architecture, the audit logging,
the encryption choices — but formal attestations come on a
schedule. We'll tell you where we are and when the next audit is.

---

## What a healthy first month looks like

If Daalu is the right tool for you, the first month should look
something like this:

**Week 1.** One person connects the cloud accounts and at least one
observability source. They configure Slack notifications and a
basic alert rule. The Assistant becomes a daily-use tool for
"what's going on right now."

**Week 2.** Two or three more people log in. They use the *Alerts*
page during a real incident and discover the explanation panel.
Someone approves the first real change proposal.

**Week 3.** Cluster federation is set up for one on-prem cluster.
The Assistant gains visibility into pod state and logs. A flaky
alert gets a custom runbook attached to it.

**Week 4.** A workflow is authored to automate a recurring task
(daily disk-space check, weekly cert-expiry sweep, ad-hoc on-call
handoff briefing). The team's Monday-morning ritual moves from
five tools to one.

If by the end of the month you're still bouncing between tabs the
way you were before, something is wrong — book a call. The pattern
of adoption is unusually predictable; when it doesn't happen, it's
almost always a missing integration or a misconfigured alert rule.

---

## What this book assumes about you

The rest of this handbook assumes you know what an alert is, what
Kubernetes pods are, what an API token looks like, and how to copy
a value from one tab to another. We don't assume you've used Daalu
before, that you've used any specific competitor before, or that
you've ever deployed a Helm chart.

When a feature requires kubectl or Helm — and a few do, for the
edge deploy — we tell you exactly what to type and where to type
it. Chapter 34 is the deepest you'll go, and even that's just a
copy-paste-and-watch.

Ready? Chapter 4 gets you signed up.
