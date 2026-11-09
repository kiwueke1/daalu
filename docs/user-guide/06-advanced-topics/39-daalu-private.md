# 39. Daalu Private

Most Daalu tenants are on the **Standard** tier — telemetry transits
our hub's process memory during a request, prompts go to your local
GPU (or daalu-hosted, or cloud) per your router config, and
tenant-scoped rows (alerts, change proposals, briefings) live in
our database. That works for the vast majority of buyers and is
covered by the privacy guarantees in Chapter 38.

A small minority — regulated industries, government, healthcare,
finance — can't sign off on "transits Daalu's hub at all." This
chapter is about that tier: **Daalu Private**. It's two settings
you can turn on, independently or together, and a 3–5× price
premium.

---

## What changes

| | Standard | Private (edge agents) | Private (full) |
|---|---|---|---|
| Where your telemetry is read | Daalu hub | Your cluster | Your cluster |
| Where the AI agent runs | Daalu hub | Your cluster | Your cluster |
| Where alerts / proposals / briefings are stored | Daalu hub DB | Daalu hub DB | **Your DB** |
| Where conversation history lives | Daalu hub DB | Daalu hub DB | **Your DB** |
| Where event records live | Daalu hub DB | Daalu hub DB | **Your DB** |
| Where integration configs (incl. secrets) live | Daalu hub (encrypted) | Daalu hub (encrypted) | **Your DB (encrypted)** |
| What Daalu hub sees in process memory | Full telemetry | Synthesised summary | Pass-through bytes only |
| LLM inference | Per router | Own GPU or daalu-hosted only | Own GPU or daalu-hosted only |

The two switches:

* **Edge-side agents** — the orchestration loop (alert-explainer,
  briefing, RCA, drift remediation) moves into a pod that runs
  inside your `daalu-edge` cluster. The agent reads your
  Prometheus / Loki / kubectl *directly*; the result that comes back
  to the hub is the agent's synthesised English/JSON answer, not
  the raw data it consulted.
* **Customer-managed Postgres** *(now real — "Private (full)")* — every
  tenant-scoped table (alerts, events, change proposals,
  conversation history, briefings, integration configs, workspaces)
  lives in a Postgres on your cluster. The Daalu hub becomes a
  pure control plane: it serves the UI shell, holds your account
  + billing rows, mints service tokens, and **proxies your
  browser's API calls through the WireGuard tunnel** to a backend
  that runs inside *your* cluster. Tenant rows never land on the
  hub's disk and only transit hub process memory the way they
  would transit a load balancer.

You can take just the first switch if you want to keep the
operational simplicity of "Daalu hosts our DB" but stop telemetry
from transiting our process memory in *interpreted* form. You take
both for the strictest data-residency contracts.

The full-tier setup is **self-serve in a single Helm command** —
see "How to enable it" below.

---

## What it does NOT change

Daalu Private is not air-gapped operation. Your edge still needs a
WireGuard tunnel to Daalu's hub for the **control plane**: pushing
the latest agent config, fetching tenant settings, sending us your
billing telemetry, serving your team the operator UI. That traffic
is small (~MB/month per tenant), carries no operational telemetry,
and goes through the same tunnel you already have.

If you require literal air-gap, run the whole operator app
inside your own boundary (it is self-hostable).

---

## When this is the right tier

Pick Daalu Private when:

* **Your legal team flagged Daalu's data-handling clause.** A
  Private tenant doesn't generate any prompts or telemetry data
  inside Daalu's processes — there's nothing to clause about.
* **You have a contract that says "data stays on our hardware."**
  Edge-side agents satisfy that for telemetry; customer-managed
  Postgres satisfies it for stored tenant rows.
* **You operate in a regulated industry** — HIPAA, PCI, SOX, GDPR
  data-residency.
* **Your dataset is uniquely sensitive.** Some operations teams
  treat their *configs* (Terraform, Helm values, secrets paths)
  as more sensitive than their actual data — Private keeps both
  inside your boundary.

Stick with **Standard** when:

* You're a startup or mid-market customer who doesn't have a
  specific compliance constraint. Standard is much cheaper and
  almost always sufficient.
* You don't want to run another Postgres or another agent pod.
  Private isn't free even on your side — there's real operational
  weight.
* Your usage pattern is "occasional ops chat" rather than
  "agents running 24/7 against production telemetry."

---

## How to enable it

Two operator actions, one on the hub side and one on yours.

**On the hub:** there's no switch for this in the app. Ask your
operator to enable Daalu Private (full)
for your organisation. They give you the one-time service token
and your tenant ID that the Helm overrides below need. Store the
token straight away; it can't be shown again.

**On your cluster:** upgrade the daalu-edge chart with the
overrides. The single-command form:

```bash
helm upgrade daalu-edge daalu/daalu-edge \
  --reuse-values \
  --set tenantData.enabled=true \
  --set tenantData.tenantId=<your-tenant-uuid> \
  --set tenantData.serviceToken=<the cleartext> \
  --set postgres.enabled=true \
  --set postgres.password=<your choice; one-time> \
  --wait
```

That installs:

* A Postgres StatefulSet (one pod, one PVC, default 50 Gi) — bundled
  for the smooth onboarding path. Production users typically swap
  this for a managed Postgres (RDS / CNPG / Cloud SQL) by setting
  `postgres.enabled=false` and `tenantData.dbUrl=<their URL>`.
* `daalu-edge-data` — the per-tenant backend that the hub forwards
  to. Same Daalu image as the rest of the platform; just runs with
  `--mode=edge` so it serves tenant-scoped CRUD against the local
  Postgres and rejects anything else.
* A one-shot migration Job that lays down the schema on first
  install. ~30 seconds.
* NetworkPolicies that keep Postgres reachable only from
  daalu-edge-data, and daalu-edge-data reachable only from the
  daalu-edge tunnel pod.

Total time on your side, on a cluster that already has
daalu-edge installed: about 60 seconds. After the rollout, your
next click in the operator UI lands at your edge.

To check it, `kubectl -n daalu-edge get pods` should show the
edge-data pod and Postgres running, and your operator can confirm
from the hub side that requests are reaching your edge.

---

## What still goes through Daalu's hub

Even with Private at maximum, the following traffic still crosses:

* **Your operator UI.** Logging into `ops.example.com` is a hub call;
  rendering the dashboard pulls from your DB (via the tunnel) but
  the browser sees it. If your contract requires the browser
  itself to never load Daalu HTML, Private Enclave is the tier.
* **Billing telemetry.** We need to know how many tokens you used
  and how many seats you have. The hub receives token counts and
  user IDs — nothing else.
* **Service-token rotation.** Done by the hub on a schedule.
* **The agent's synthesised answer.** If your agent says "memory
  on api-3 looks fine," that string lands on the hub long enough
  to render in the UI. It does *not* land in the DB if you have
  customer-managed Postgres.

That's the full list. If any of those are not OK for your contract,
Private isn't the tier you want — talk to us about Enclave.

---

## Limitations

* **Slower agent startup.** First agent run after a pod restart
  takes ~30 s while it warms up. Hub-side agents share warmth across
  tenants; your edge agent does not.
* **You operate the DB.** Backups, replication, patching, HA — all
  on your side. We publish an RDS Terraform module and a CNPG
  Helm chart as reference, but the contract is "you operate it."
* **No multi-tenant cost savings on your side.** The DB and the
  edge-agents pod are sized for one tenant. There's no shared
  infra to amortise across.
* **Some features lag.** New agent types ship to Standard first
  (because the hub deploys monthly); Private edge pods follow on
  the next chart bump. You're typically 1–4 weeks behind.

---

## See also

* Chapter 34 — Deploying daalu-edge (the chart that hosts the
  edge agents)
* Chapter 38 — The daalu-hosted AI tier (the alternative for buyers
  who want some privacy without the operational lift of Private)
* Chapter 12 — Tenancy concepts
* Chapter 18 — Change proposals (the agent's only path to changing
  your infrastructure, regardless of tier)
