# 46. Glossary

Every term defined in one place. Cross-referenced where
useful.

---

**adapter**
The internal piece of code in Daalu that talks to one specific
external system. Each integration uses one adapter. See
Chapter 10.

**admin**
A user with `is_admin=true` on the tenant. Can manage users,
integrations, and tenant settings. See Chapter 9.

**agent**
A configured autonomous behaviour that runs on a schedule or
trigger. Examples: the briefing agent, the reconciler, the
alert-explainer. See Chapter 19.

**AI Factory**
The GPU observability page — live utilisation, thermals, and
the fleet-wide diagnostics/benchmark/reliability operations.
See Chapter 50.

**alert**
A signal worth a human's attention. Created when an event
matches an alert rule. See Chapter 11.

**alert rule**
The recipe that promotes events into alerts. Defined either
in your observability stack or in Daalu's *Settings → Alert
rules*.

**API token** — see *personal access token*.

**Assistant**
The AI co-pilot. A chat panel that can investigate, propose,
and notify. See Chapter 12.

**audit log**
Tenant-wide log of every state-changing action. Admin-only.
See Chapter 24.

**bootstrap token**
The one-shot credential used to set up cluster federation.
Expires after 1 hour. See Chapter 34.

**break-glass admin**
The one local administrator account on a management
environment's source-of-truth console, for when single sign-on
itself is unavailable. Chosen (or generated) at environment
creation and shown exactly once. See Chapter 27.

**briefing**
The auto-generated summary on the home page. Runs daily. See
Chapter 5 and Chapter 19.

**briefing time**
The hour at which the briefing agent runs in your tenant's
time zone. Configurable.

**chat (tier)**
The default LLM SKU for typical Assistant turns. See Chapter
41.

**bundled source of truth**
The Nautobot instance included in every management environment,
wired to Daalu automatically — no URL or token to paste. See
Chapter 27 and Chapter 30.

**change proposal**
A structured request to mutate state, awaiting human approval
before execution. The central safety primitive in Daalu. See
Chapter 18.

**classifier (tier)**
A small, cheap LLM SKU for routing/classification decisions.
See Chapter 36.

**cluster federation**
The pattern by which Daalu reaches your on-prem/private
Kubernetes clusters through an outbound WireGuard tunnel. See
Chapter 14.

**cost-anomaly agent**
The shipped agent that flags significant spend deviations.
See Chapter 19.

**daalu-edge**
The Helm chart you install in your cluster to enable
federation. See Chapter 34.

**daalu-vllm**
The open serving stack Daalu deploys onto your GPU node when
you connect a GPU — you don't install it yourself. See
Chapter 35.

**dashboard**
A Grafana-defined board rendered natively inside Daalu, its
panels filled from your own Prometheus/Loki. Grafana stays the
catalog; Daalu does the rendering. See Chapter 48.

**device terminal**
The in-browser SSH session to a fabric device, opened from the
topology view. Uses the tenant's stored device credential;
reaches private management IPs through the cluster tunnel. See
Chapter 49.

**drift**
A difference between the source-of-truth and the live state
of a device. Detected by the reconciler. See Chapter 13.

**event**
The lowest-level observation. Every adapter emits events.
Some events become alerts. See Chapter 11.

**executor**
The internal Daalu service with credentials to mutate
external state. Acts only on approved change proposals. See
Chapter 18.

**federation tunnel**
The WireGuard connection between your cluster and the Daalu
hub. See Chapter 14.

**four-eyes rule**
The constraint that a user cannot approve a change proposal
they (or the Assistant on their behalf) proposed. See Chapter
6.

**hub**
The Daalu-side WireGuard server that terminates federation
tunnels. Operated by us; you don't see it directly. See
Chapter 34.

**incident**
A grouping of alerts under a single ongoing investigation.
Has a timeline and a postmortem. See Chapter 20.

**ingest API key**
A separate credential class used by `POST /api/v1/events` and
similar webhook endpoints. Different from PATs. See Chapter
33.

**integration**
Daalu's connection to one of your external systems. The unit
of configuration on the Managed Infra page. See Chapter 10.

**invite**
The single-use link sent to a teammate to join your tenant.
Expires in 7 days. See Chapter 6.

**JWT**
The short-lived token issued at login. Used for browser
sessions. Different from PATs. See Chapter 37.

**label**
A key=value tag on an alert, event, or integration. Used for
routing and filtering. See Chapter 11.

**LLDP**
Link Layer Discovery Protocol — how network devices announce
themselves to their neighbours. Daalu reads devices' live LLDP
tables to draw the observed side of the topology map. See
Chapter 49.

**LLM router**
The internal component that decides where each inference
call goes. See Chapter 36.

**management environment**
One named, independent instance of the management stack
(`Production`, `Lab`, …), running in its own namespace with its
own consoles. See Chapter 27.

**management stack**
The set of services Daalu provisions to manage your network and
servers: source of truth, workflow engine, config store and
renderer, and optional zero-touch provisioning and DHCP. See
Chapter 27.

**local inference**
Inference served by your own GPU rather than a commercial
LLM provider. See Chapter 15 and Chapter 35.

**Nautobot**
The currently-supported source-of-truth system. See Chapter
30.

**notification**
The outbound message Daalu sends to get a human's attention.
Goes through Slack, PagerDuty, email, browser push. See
Chapter 11.

**PAT** — see *personal access token*.

**personal access token (PAT)**
The durable credential for scripts and CI. Format:
`dpat_xxxxxxxxxxxxxxxxxxxxxxxx`. See Chapter 37.

**primary environment**
The management environment the rest of Daalu binds to by
default — Operations, topology, and device onboarding all read
its source of truth. Marked with a *Primary* badge. See
Chapter 27.

**proposal** — see *change proposal*.

**reconciler**
The shipped agent that compares source-of-truth with live
device state and writes drift proposals. See Chapter 13.

**role**
One of *admin* or *user*. See Chapter 9.

**runbook**
Documentation attached to an alert rule describing what to
do when it fires. See Chapter 11.

**severity**
One of *info*, *warning*, *critical*. See Chapter 11.

**SKU**
A class of inference call: classifier, chat, chat-large,
embedding. See Chapter 22.

**snooze**
Suppressing notifications for an alert without resolving it.
See Chapter 20.

**source of truth (SoT)**
Your authoritative inventory system. Currently Nautobot. See
Chapter 13.

**tenant**
Your company's workspace inside Daalu. Isolated from other
tenants. See Chapter 9.

**tier (LLM)** — see *SKU*.

**tool**
A specific function the Assistant can call. Some read-only,
some via proposal. See Chapter 12.

**topology (fabric topology)**
The live network map: nodes from the source of truth, links
coloured by observed LLDP adjacency and telemetry. See
Chapter 49.

**tunnel** — see *federation tunnel*.

**user**
Anyone with a Daalu account. May be admin or non-admin. See
Chapter 6.

**Watchdog**
The always-firing test alert that verifies the alert
pipeline. See Chapter 20.

**webhook**
HTTP-based integration for arbitrary external systems.
Inbound or outbound. See Chapter 33.

**workflow**
A user-authored automation. Becomes an agent once active.
See Chapter 21.

---

End of the User Guide.

For the engineering / operations companion, see the
*Engineering & Operations Manual* in the sibling
`docs/book-engineer/` directory.
