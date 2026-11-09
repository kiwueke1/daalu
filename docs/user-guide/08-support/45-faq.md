# 45. Frequently asked questions

The questions we get asked the most, with honest answers.

---

## Security and compliance

**Where is my data stored?**
In the operator cluster's Postgres, wherever you (or your
operator) run it. Data that lives in *your* infrastructure — the management
stack, your GPU's prompts, your metrics stores — stays there;
Daalu reaches it over the tunnel rather than copying it out.

**Is data encrypted at rest?**
Yes, AES-256 on volumes and tablespaces. Secrets are
additionally encrypted in the application layer.

**Is data encrypted in transit?**
Yes, TLS 1.3 everywhere. WireGuard for the federation tunnel.

**Who can see my data?**
Whoever operates your Daalu deployment. The platform runs no
automated analytics on tenant data. Details in Chapter 57 of
the engineer book.

**Can I review the platform's source code?**
Yes. The whole platform — operator app, daalu-edge, the
`daalu` CLI, and the GPU serving stack — is open source under
Apache-2.0 at `https://github.com/kiwueke1/daalu`.

**What about GDPR / CCPA?**
Compliance is the operator's responsibility; data-export and
right-to-be-forgotten are exposed at *Settings → Tenant → Privacy.*

---

## Architecture

**Why WireGuard for federation, not VPN-as-a-service X?**
Outbound-only, auditable kernel-level implementation,
universally available, and not a vendor we have to depend
on. The tradeoff is operational complexity inside the edge
pod, which we absorb.

**Can I self-host the operator app?**
Yes — it ships as a Helm chart with a one-shot install
script; see the engineering manual, Part VIII.

**Why Postgres, not <other DB>?**
Boring choice. It works. Back it up with volume snapshots
and/or a nightly `pg_dump`.

**Does Daalu have a public API?**
Yes; see Chapter 37 and `https://ops.example.com/docs`.

---

## AI Assistant

**What model does the Assistant use?**
Depends on the routing decision (Chapter 36). Could be a
local 8B/70B, Claude, GPT, or something else. The reply
detail shows which.

**Does the Assistant have my source code?**
No. It has the data your integrations expose. It can read
logs through Loki, metrics through Prometheus, alerts and
events through Daalu's own model. It does not crawl your
repositories.

**Can the Assistant call AWS directly?**
Only with your written-tool permission. Even then, write
operations go through the proposal mechanic.

**Why does the Assistant sometimes refuse to do something?**
Several reasons:
- The action requires write permission we don't have.
- The action is destructive and requires explicit
  confirmation in chat ("yes, I really want to delete this").
- The Assistant doesn't have a tool to do it.

**Why does the Assistant occasionally give wrong information?**
LLMs do. Cite the sources in their answers and double-check
when stakes are high. Feedback via *Help → Feedback*
genuinely improves the system over time.

---

## Sign-in and accounts

**Do I need a separate login for the managed tool consoles?**
No. Your Daalu session is single sign-on across the product
*and* the consoles of the managed services Daalu provisions for
you (the network management stack's source-of-truth, workflow,
and config UIs). Log in once; every console you open from Daalu
already knows who you are.

**Then what is the "break-glass admin" I was shown at
environment creation?**
A local emergency account on the source-of-truth console, for
the day single sign-on itself is unavailable. It's shown
exactly once, at creation — store it in your password manager
and forget about it. You never need it day to day.

**Can I sign in with Google?**
Yes — **Continue with Google** on the sign-in page. Email +
password also works for invited and self-signed-up accounts.

**I clicked "Create an account" and it says account creation
isn't available.**
Self-service sign-up is a per-instance setting your operator
may not have enabled. Ask an admin on your team for an invite.

---

## Network & server management

**Do you install anything of yours in my cluster besides the
edge agent?**
Only what you ask for: creating a management environment
installs that stack into a namespace in your cluster, and
connecting a GPU deploys the serving stack. Both are visible,
inspectable workloads in your own cluster — and both are
removed by the corresponding Disconnect/Delete in the UI.

**Can I run more than one management environment?**
Yes — environments are named and independent (`Production`,
`Lab`, …). One is marked **Primary**; that's the one Operations
and the topology view bind to. See Chapter 27.

**Why can't I rename an environment?**
The name becomes the namespace and the public hostnames, which
are baked into live URLs. Create a new environment instead.

**My devices only have private management IPs. Can Daalu still
manage them?**
Yes. Config push, drift checks, the topology probe, and the
in-browser terminal all dial devices through your cluster
tunnel. Nothing needs to be exposed.

---

## Source of Truth

**Does Daalu support Netbox?**
Not yet. Nautobot only. Netbox was on the original roadmap
but is currently deferred while we focus on Nautobot
depth.

**Does Daalu support [my CMDB]?**
Probably yes via the webhooks integration. Talk to support
about a specific adapter if it's a common one.

**Can Daalu push to my switches without root credentials?**
The credentials are whatever you configured Daalu with —
either pulled from Nautobot's secrets store or set in
*Settings → Tenant → Device credentials*. Use the
narrowest set of perms the operations you want need.

---

## Local GPU

**Do I need a Kubernetes cluster to use local inference?**
Yes — a cluster with the NVIDIA GPU Operator, connected via
daalu-edge. From there, adding the GPU is a single form
(Chapter 35); Daalu discovers the card and deploys the serving
stack itself.

**Do I have to label nodes or paste a kubeconfig?**
No. That used to be true; it isn't any more. The GPU class is
derived automatically from what the cluster reports about the
card, and the edge registers the cluster credential itself
during onboarding.

**Can I use a workstation as a GPU node?**
Yes, if it's running k3s/microk8s and is reachable through
the federation tunnel. We have customers running this from
a single beefy workstation in an office closet.

**What if my GPU is busy with other workloads?**
The serving pod requests its share via Kubernetes scheduling.
If the GPU is fully utilized, it waits or pre-empts based
on your PriorityClass.

**Can I serve a custom-fine-tuned model?**
Not through the catalog UI; add an entry to the model
catalog (engineering manual, chapter 73).

**I connected a GPU but AI Factory says no GPUs are reporting
metrics.**
Serving and metrics are independent pipelines — connect the
cluster's Prometheus (Chapter 8) and run the **Observability
check** tile on the AI Factory page to find the quiet layer.

---

## Migrations and data portability

**How do I export my data out of Daalu?**
*Settings → Tenant → Export.* You get a JSON dump of
everything: integrations, alerts, proposals, audit logs.

---

## What's next

Chapter 46 is the glossary — every term defined in one place.
