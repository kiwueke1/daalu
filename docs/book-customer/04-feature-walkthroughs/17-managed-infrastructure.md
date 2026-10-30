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

- **Clusters & observability** — your federated Kubernetes
  clusters, each clickable through to a detail page with a
  health overview and a read-only kubectl runner. Covered in
  this chapter.
- **Network & servers** — the managed network/server management
  environments Daalu provisions into your clusters. Chapter 27
  is the full walkthrough.
- **Onboarding** — the guided integration wizard, for
  first-time setup of any source.
- **Integrations** — every external system connection: cloud
  accounts, Prometheus / Loki / Thanos / OpenSearch / Grafana,
  Slack and PagerDuty, webhooks, and a bring-your-own source of
  truth.

> **Where did the Integrations sidebar entry go?** It's this
> last tab. Wiring external systems is managed-infra work, so
> the standalone page folded in here — the old `/integrations`
> URL still works and redirects.

---

## A row, in detail

Every row across every section has the same shape:

- **Status badge** (green / amber / red / gray). Gray means
  "configured but never tested" — usually the first 30 seconds
  after creation.
- **Name** — the friendly name you gave at setup.
- **Type** — the underlying integration type (`aws`, `nautobot`,
  `slack-channel`, etc.).
- **Last check** — when Daalu last probed the integration.
- **Scope** — a short string describing what's in scope ("3
  regions," "all subscriptions," "channel #netops").
- **Actions** (⋯) — opens a menu: Edit, Test now, Disable,
  Delete.

Click anywhere on the row to open its detail panel.

---

## Cloud Accounts section

Each row shows the cloud, account ID, and a count of inventory
detected:

```
[green]  Acme prod AWS     account: 123…012      EC2: 41, S3: 28, RDS: 5
[green]  Acme prod GCP     project: acme-prod    GCE: 12, GCS: 8
[red]    Acme staging      account: 234…123      credentials expired
```

Clicking opens a detail page with:

- **Inventory** tab — every resource Daalu has crawled, filterable
  and sortable. Includes ARN/URI, region, tags, last-seen.
- **Cost** tab — cost over time pulled from the cloud's billing
  API. Aggregations by service, region, tag.
- **Health** tab — recent probe history with timestamps and
  responses.
- **Settings** tab — friendly name, role ARN, scope, "Verify
  now" button.

The **Add cloud account** button at the top of the section
opens the wizard from Chapter 7.

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

### The cluster detail page

The page header shows the cluster's name, connection status,
tunnel IP, and when the tunnel last handshook. Below that:

- **Overview cards** — the Kubernetes server version, the number
  of nodes, and the number of namespaces.
- **Nodes table** — every node with its status (Ready /
  NotReady), roles, kubelet version, internal IP, CPU and memory
  capacity, and age.

If the cluster isn't reachable yet — the tunnel is still coming
up, or no Kubernetes credentials have been linked — the page
says so instead of showing stale numbers, and the command runner
below is disabled until it's back.

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
- **Related consoles and dashboards** — jump-offs to the metric
  console, the logs console, the GPU & AI Factory view, and (if
  Grafana is connected — Chapter 48) the dashboards relevant to
  this cluster.

Each health collector degrades independently: if one read is
forbidden or times out, that metric is marked partial instead of
blanking the whole overview.

### Running kubectl commands on a cluster

Underneath the overview is the **kubectl commands** panel. This
lets you run a curated set of *read-only* commands against the
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
4. **Click Run.** Daalu executes each command on the cluster and
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

## Observability Stacks section

One row per Prometheus / Loki / Thanos / OpenSearch instance:

```
[green]  prom-eu-prod        URL: http://prom.eu-prod.svc:9090   active series: 1.4M
[green]  loki-main           URL: through-tunnel.svc:3100         ingest: 240 GiB/d
```

Clicking opens:

- **Connection** — how Daalu reaches this stack (direct URL, or
  through a federation tunnel).
- **Probe history** — recent query latencies, errors.
- **Routes** — what classes of query Daalu sends to this stack.
  Useful when you have multiple Prometheuses and want to know
  which one the Assistant is hitting.

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

## Source of Truth section

For now, this is Nautobot rows (Netbox deferred):

```
[green]  Acme Nautobot       URL: nautobot.acme.io      sites: 8, devices: 412
```

Clicking opens:

- **Inventory summary** — sites, device kinds, vendors.
- **Reconciliation** — schedule, last run, current drift count.
- **Credentials** — token age, "Rotate" button.

The **Add Source of Truth** button walks through the Nautobot
setup (Chapter 30).

---

## Notifications section

Configured outbound channels:

```
[green]  Slack workspace acme.slack.com   channels: #ops-alerts, #netops
[green]  PagerDuty               service: platform-prod (PXX1234)
[gray]   Email                   transactional only
```

Clicking opens:

- **Channels** — which Slack channels / PD services / email
  addresses Daalu may send to.
- **Test** — fires a test notification.
- **History** — last 100 notifications sent, with status.

The configuration here is *availability*; *routing* (which alert
goes where) lives in *Settings → Notifications*.

---

## Webhooks section

Two flavors:

- **Inbound** — Daalu hosts a URL per integration; you POST
  events to it. Useful for CI pipelines, custom alerters, or
  anything that wants to write to Daalu's timeline.
- **Outbound** — Daalu POSTs to your URL on configured triggers.
  Useful for piping Daalu events to your own warehouse.

Each row shows: URL, last delivery, success rate over 24 hours,
secret rotation status.

---

## Adding an integration

Every section has a green **+ Add** button. The flow is the
same:

1. Pick a type from a tile grid.
2. Fill the type-specific wizard (Chapter 28–33 cover each).
3. Daalu does a synchronous test call. Pass → row appears
   green. Fail → see the error inline; nothing is saved.

---

## Bulk operations

For tenants with many integrations:

- **Test all** — runs the health check across every row in the
  page. Useful after a credentials rotation.
- **Export** — downloads a CSV/JSON of every integration's
  status. Handy for compliance reviews.
- **Bulk disable / re-enable** — turns probes off for selected
  rows without deleting them. Their data freezes in place.

These are in the top-right kebab menu of the page.

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
