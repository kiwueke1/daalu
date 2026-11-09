# 17. Managed Infrastructure

The *Managed infra* page is the single home for everything you
wire into Daalu and everything Daalu runs for you. Cloud
accounts, Kubernetes clusters, observability stacks, the network
management stack, sources of truth, notification channels — they
all live here. This chapter walks through the page tab by tab.

URL: `https://ops.example.com/managed-infra`.

---

## Layout — four tabs

The page is organised as four tabs along the top (deep-linkable
via `?tab=…`, so a shared link lands on the right one):

- **Clusters & observability** — your cloud accounts (AWS, GCP,
  Azure), your observability stacks (Prometheus, Loki, Thanos,
  OpenSearch, Grafana), and your federated Kubernetes clusters,
  each cluster clickable through to a detail page with a health
  overview and a read-only kubectl runner. Covered in this
  chapter.
- **Network & servers** — the managed network/server management
  environments Daalu provisions into your clusters. Chapter 27
  is the full walkthrough.
- **Onboarding** — the guided integration wizard, for
  first-time setup of any source.
- **Integrations** — the guided Source of Truth (Nautobot)
  setup, notification channels (Slack, email), and the ingest
  sources that bring alerts and events into Daalu.

> **Where did the Integrations sidebar entry go?** It's this
> last tab. Wiring external systems is managed-infra work, so
> the standalone page folded in here — the old `/integrations`
> URL still works and redirects.

---

## A row, in detail

The cloud-account and observability rows on *Clusters &
observability* share one shape:

- **Provider** — what it is (*AWS account*, *Prometheus /
  Alertmanager*, *Loki (logs)*, …) and a short summary of how it's
  configured.
- **Status** — not configured (grey), configured but not yet reporting (amber), connected (green), or error (red).
- **Detail** — the key setting, such as the region or URL.
- **Actions** on the right:
  - **Connect** (or **Edit** once it's set up) opens the
    connection form.
  - **Open ›** (connected observability stacks only) opens its
    console.
  - The **bin icon** disconnects it. It asks you to confirm in
    place and says what happens — the agent loses read access —
    before anything is removed.

---

## Cloud accounts (on the Clusters & observability tab)

One row each for **AWS account**, **Google Cloud project** and
**Azure subscription** — one connection per provider. A connected
row shows its key setting (such as the default region) under
**Detail**. Click **Connect** to set one up (Chapter 7 walks
through each), **Edit** to change the credential, or the bin icon
to disconnect.

Cloud connections are read-only. The AI uses them to look things
up while it investigates an alert; on AWS, firing CloudWatch
alarms also come in as alerts.

---

## Clusters (on the Clusters & observability tab)

Each row is a federated cluster:

```
[green]  acme-us-prod             tunnel: 10.200.0.2   handshake: 12s ago
[amber]  acme-eu-prod             tunnel: 10.200.0.3   awaiting handshake
```

Click a cluster's name (or the **Open ›** link on the right of
the row) to open its detail page at
`https://ops.example.com/clusters/<slug>`. The **Onboard cluster**
button generates a one-shot bootstrap invite (Chapter 34).

**Tear down** on a row removes the cluster's tunnel. It asks you
to confirm in place (**Tear down <slug>**) and says what happens:
Daalu stops reaching this cluster.

### The cluster detail page

The page header shows the cluster's name, connection status,
tunnel IP, and when the tunnel last handshook. Below that are
three tabs: **Overview**, **Run kubectl** and **Dashboards**. The
Overview tab shows:

- **Overview cards** — the Kubernetes server version, the number
  of nodes, and the number of namespaces.
- **Nodes table** — every node with its status (Ready /
  NotReady), roles, kubelet version, internal IP, CPU and memory
  capacity, and age.

If the cluster isn't reachable yet — the tunnel is still coming
up, or no Kubernetes credentials have been linked — the page
says so in a plain sentence, with what to do next, instead of
showing stale numbers. The raw reason is under **Show details**.
The command runner is disabled until the cluster is back, and its
button says why.

### The health overview

Below the overview cards, the page shows a cluster-wide **health
rollup** — the "is anything wrong anywhere?" read you'd
otherwise assemble from half a dozen kubectl commands:

- **Troubleshooting tiles** — the counts that matter when
  something's off: *Symptomatic pods* (not Running / Succeeded),
  *Deployments w/ unavailable replicas*, *Container restarts*,
  *Unready nodes*, *Unbound volumes*, and *DaemonSets w/
  unavailable pods*. Quiet when zero; a tile lights up when it
  isn't.
- **A resource grid** — totals for pods (running / pending /
  failed), Deployments, StatefulSets, DaemonSets, ReplicaSets,
  and Services.
- **A symptomatic-pods table** — the actual offending pods, with
  namespace, phase, reason, and restart count, so your next
  click is diagnosis rather than discovery.
- **Related consoles and dashboards** — on the **Dashboards**
  tab: jump-offs to the metric console, the logs console, the GPU
  & AI Factory view, and (if Grafana is connected — Chapter 48)
  the dashboards relevant to this cluster.

Each health collector degrades independently: if one read is
forbidden or times out, that metric is marked partial instead of
blanking the whole overview.

### Running kubectl commands on a cluster

The **Run kubectl** tab lets you run a curated set of *read-only* commands against the
cluster, straight through the tunnel, without leaving the
browser or holding a kubeconfig yourself.

How it works:

1. **Tick one or more commands.** They're grouped by category —
   *Cluster* (nodes, namespaces, events, node resource usage),
   *Workloads* (pods, deployments, statefulsets, daemonsets,
   jobs, cronjobs…), *Networking* (services, endpoints,
   ingresses), *Storage* (PVCs, PVs, storage classes), and
   *Config* (configmaps). Each shows the exact `kubectl`
   equivalent underneath its label.
2. **Optionally scope it.** Type a **namespace** to restrict
   namespaced commands to one namespace (leave blank for all
   namespaces), and/or a **label selector** like `app=checkout`.
3. **Pick an output format** — *CLI table* (the familiar
   columns `kubectl get` prints), *JSON*, or *YAML*.
4. **Click Run N read-only commands.** Daalu executes each command on the cluster and
   shows the output in its own panel, with a **Copy** button.

Everything in this panel is read-only — there is no way to
change, restart, scale, or delete anything from here. (Those
actions exist, but they live behind the Assistant's
approval-gated tools in *Alerts* and *Operations*, where each
change is previewed and you click Approve before it runs.)
Secrets are intentionally not in the command list, so their
names and contents never appear here.

Who can use it: any signed-in member of the tenant can open a
cluster and run these read-only commands. Every run is recorded
in Daalu's audit log with who ran what.

---

## Observability (on the Clusters & observability tab)

One row each for **Prometheus / Alertmanager**, **Loki (logs)**,
**Thanos (long-history metrics)**, **OpenSearch (logs)** and
**Grafana (dashboards)**:

A connected row shows its URL under **Detail**. **Edit** opens the
same form you connected it with — that's where you see and change
how Daalu reaches the stack (a direct URL, or through your
cluster tunnel) and re-run **Test connection**.

Observability integrations are how the Assistant gets data for
"why did alert X fire" questions. Without at least one
configured, those questions get short answers.

### The metric and log consoles

Every connected Prometheus, Thanos, or Loki row carries an
**Open** link into a read-only console at
`/observability/<provider>` — the observability sibling of the
kubectl runner above:

- **Overview** — headline health read straight from the store:
  targets up/down, currently-firing alerts, build version, and
  per-job health for a metrics store; label and namespace
  inventory for Loki.
- **Query console** — tick one or more ready-made PromQL/LogQL
  panels, pick a time window, and run them. The metrics console
  also accepts one free-form PromQL expression; the logs console
  lets you narrow to a namespace and a search term.

Everything is a read — nothing in these consoles can mutate the
store — and it all works through the federation tunnel, so an
in-cluster-only Prometheus is fully browsable from here.

For Grafana-defined dashboards rendered natively, see the
**Dashboards** page (Chapter 48).

---

## The Integrations tab

The fourth tab holds three groups:

- **Guided setup** — **Source of Truth (Nautobot)**, a three-step
  guide: pick hosted or bring-your-own Nautobot, wire the
  connection, set up SSH credentials, then add your first device.
  Chapter 30 covers it.
- **Notification channels** — **Slack notifications** (where
  Daalu posts daily briefings and incident updates) and **Email
  (SMTP)** (on-call escalations, optional daily-briefing emails,
  and the invites you send). Each card says whether it's
  configured, with **Set up …** or **Edit …**. Which messages
  reach *you* is your choice, in *Settings → Notifications*.
- **Ingest sources** — where Daalu's alerts and events come from:
  Prometheus / Alertmanager, PagerDuty, AWS (CloudWatch alarms),
  Loki, Thanos and OpenSearch. Each card shows whether it's set
  up, and **Check now** pulls new alerts from it immediately
  instead of waiting for the next scheduled pass. **Demo
  sources** (folded away at the bottom) feed sample data for
  trying Daalu out.

Anything else that can make an HTTP request can post events to
Daalu directly — see Chapter 33.

---

## Adding an integration

Every provider row has a **Connect** button (on the Integrations
tab, **Set up …**). The flow is the same:

1. Click **Connect** on the provider you want.
2. Fill the type-specific form (Chapter 28–33 cover each).
3. Click **Test connection**. Daalu does a synchronous test call
   and shows the result inline.
4. Click **Connect <provider>** to save it (**Save changes** when
   editing).

---

## The Network & servers tab

The second tab holds the **management environments** — the
dedicated network/server management stacks Daalu provisions into
your clusters, shown as a tile per environment with live install
progress, upgrade controls, and the device-onboarding panel.
It's a feature of its own; Chapter 27 walks through it
end to end.

---

## What's next

Chapter 18 covers the *Operations* page — the network device,
drift, and change-proposal hub. It's the page that the
Assistant most heavily interacts with in NetOps contexts.
Chapter 48 covers Dashboards, and Chapter 49 the live fabric
topology.
