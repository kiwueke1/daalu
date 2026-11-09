# 8. Onboarding your observability stack

This chapter walks through connecting your Prometheus, Loki,
Thanos, and OpenSearch to Daalu — so the AI Assistant can run
real queries against them, the alert-explainer agent can pull
log context, and you can mirror your alert rules into the
Daalu alerts feed.

The interesting part is *where* your observability stack lives.
There are two shapes:

1. **You already expose Grafana / Prometheus on the public
   internet** (often behind an SSO ingress). For Daalu, this
   is the easy path — paste the URL, paste any credentials,
   done.
2. **Your observability stack only listens inside your own
   Kubernetes cluster** (the common on-prem case). Daalu can
   still reach it, but only after you've onboarded that
   cluster as a managed cluster. Daalu then routes its queries
   through a tunnel to a small agent in your cluster — your
   Prometheus never leaves the cluster network.

Most teams take path #2 because they don't want to expose
monitoring on the public internet. The rest of this chapter
focuses on it. Path #1 is mentioned at the end.

---

## Before you start

A short checklist. Most of these are reversible — you can change
your mind later — but knowing them up front saves you from
re-walking the wizard.

### Which cluster?

If your environment has more than one Kubernetes cluster
(production, staging, multiple sites), pick the cluster whose
observability stack you most want Daalu to read first. Usually
that's production. You can connect more clusters later — Daalu
can hold a different Prometheus per cluster and route queries
appropriately.

### Read-only access

Daalu only reads from your observability stack — never writes,
never inserts samples, never modifies alert rules upstream.
The credentials you'll paste should be **read-only** at the
upstream service. For Loki and OpenSearch, a dedicated
service-account user scoped to your log indices is the
recommended approach.

### What gets queried, and how often

- **Prometheus / Alertmanager** — Daalu pulls firing alerts
  every couple of minutes; the Assistant runs ad-hoc
  PromQL when investigating an incident. Rate limit: ~30
  queries/minute per target.
- **Loki** — Only on-demand from the Assistant
  (`{namespace="foo"} |~ "error"` style queries). Rate limit:
  ~10 queries/minute.
- **Thanos** — Long-history PromQL (7-day, 30-day windows).
  Same per-minute budget as Prometheus.
- **OpenSearch** — On-demand DSL queries from the Assistant.
  ~30 queries/minute.

These budgets are set for your Daalu instance, not in the app. If
you need them raised, ask your operator.

---

## Step 0 — Onboard your Kubernetes cluster (prerequisite)

Skip this section if you've already done it (you'll see your
cluster in *Managed infra → Clusters & observability* with status
**connected**).

This step has to happen *first*. Daalu can't route a
Prometheus query into a cluster it doesn't have a tunnel to.
The tunnel is what lets you keep your observability stack
internal — Daalu reaches in, instead of you exposing it
outward.

### 1. Open the wizard

Sidebar → **Managed infra**, on the **Clusters & observability**
tab → **Onboard cluster.**

You'll be asked for:

- **Slug** — a DNS-style name you pick (e.g.
  `prod`, `eu-prod`, `lab-1`). It shows up in URLs and the
  pickers later.
- **Display name** — anything (e.g. "EU production").

Click **Create cluster & get install snippet.**

### 2. Daalu mints a one-shot install snippet

The next screen shows a `helm install daalu-edge ...`
snippet. It contains:

- The hub's WireGuard public key and endpoint.
- A `tunnel_ip` Daalu has allocated for your cluster on the
  shared mesh (e.g. `10.200.0.7`).
- A **one-shot invite token** valid for a single bootstrap.

**Copy this snippet now and store it somewhere safe.** The
invite token is shown exactly once. If you lose it before the
edge has used it, you have to re-onboard.

### 3. Run the snippet in your cluster

In a shell with `kubectl` and `helm` configured against your
workload cluster:

```bash
helm install daalu-edge oci://ghcr.io/daalu/charts/daalu-edge \
  --namespace daalu-edge --create-namespace \
  -f operator-provided-values.yaml
```

(The exact one-liner is in the wizard.) The chart deploys two
things into your cluster:

- A **WireGuard pod** that brings up a small UDP tunnel back
  to Daalu's hub. No inbound ports opened on your side; the
  pinhole is kept open from your side with PersistentKeepalive.
- An **edge HTTP proxy** sidecar. This is the new piece — it
  listens *only on the tunnel's private address* (e.g.
  `10.200.0.7:8888`) and forwards HTTP requests that arrive
  over the tunnel to in-cluster Services. We'll explain what
  it does in step 1 below.

By default the edge proxy will only forward to in-cluster
DNS names (`*.svc.cluster.local`) and RFC1918 addresses — so
even if something on Daalu's side went rogue, the proxy
refuses to dial public hosts. (You can widen this with the
`edgeProxy.allowedHosts` helm value if you need to.)

### 4. Wait for "connected"

Back in *Managed infra → Clusters & observability*, the row's
status pill cycles through `pending → awaiting handshake →
connected`. The full handshake usually takes under a minute.

If it sits at `awaiting handshake` for more than a few
minutes, see chapter 14 (cluster federation) for the
troubleshooting flowchart. The most common cause is the
cluster's egress firewall blocking UDP 51820.

Once it's **connected**, you can move on.

---

## Step 1 — Add Prometheus / Alertmanager

We'll use Prometheus as the worked example. Loki, Thanos, and
OpenSearch follow the exact same shape — just different URLs
and field labels.

### 1. Open the integration form

Sidebar → **Managed infra**, on the **Clusters & observability**
tab. Under **Observability**, find the **Prometheus /
Alertmanager** row and click **Connect.**

(Or: *Managed infra → Onboarding → Prometheus*. They're the same
form.)

### 2. Tick "Reachable via federation tunnel"

This is the toggle that switches the integration from
"Daalu dials the URL directly" to "Daalu dials the URL
through your edge proxy." It's hidden until you've onboarded
at least one cluster — if you don't see it, go back and check
Step 0.

When you tick it, a dropdown appears with every connected
cluster on your tenant. Pick the cluster whose Prometheus
you're adding. The dropdown shows the cluster's tunnel IP
next to its name, so you can sanity-check you've got the
right one.

A URL hint also appears underneath, e.g.:

> *URL hint: `http://prometheus.monitoring.svc.cluster.local:9090`*

### 3. Paste the in-cluster URL

Into the **base URL** field, paste the **Prometheus server**
URL the way it's reached *from inside that cluster*. For most
kube-prometheus-stack installs, that's:

```
http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090
```

> **Use the Prometheus server (`:9090`), not Alertmanager
> (`:9093`).** The server's query API backs everything — the
> Assistant's PromQL, the metric console, dashboards, *and*
> alert ingestion. If your install runs Alertmanager, Daalu
> finds and reads its alerts through the same integration; if
> it doesn't (Alertmanager disabled is a common default), Daalu
> automatically falls back to Prometheus' **own** firing-alerts
> API — a Prometheus-only cluster still feeds the alert
> pipeline. Pointing the integration at Alertmanager instead
> would break every metric query.

What goes in here depends on what your cluster's Service is
named — `kubectl get svc -n monitoring` is the truth. Daalu
does not try to guess, and two clusters rarely name it the
same way — a URL copied from a *different* cluster can pass
the connection test (some other Prometheus answered) and then
return zero data for this cluster's queries.

You don't need a username or password if your Prometheus
isn't behind auth (kube-prometheus-stack defaults to
unauthenticated inside the cluster, which is the common
case).

### 4. Click "Test connection"

What happens behind the scenes:

1. Daalu's API receives your URL + the cluster you picked.
2. It looks up the cluster's tunnel IP and dials the edge
   proxy at `http://<tunnel_ip>:8888`.
3. The proxy receives an HTTP request with your absolute URL
   on the wire. It's *inside* your cluster, so
   `*.svc.cluster.local` resolves naturally via kube-dns.
4. The proxy forwards to Prometheus; Prometheus answers; the
   response streams back through the tunnel.
5. Daalu records the round-trip time and shows a green check.

A green badge with a latency (e.g. *"endpoint responded ·
47ms"*) means the path is alive end-to-end.

Common test failures:

| Message | What it means |
|---|---|
| *"probe failed: All connection attempts failed"* | Tunnel isn't actually connected. Re-check step 0; the cluster row may have flipped to `awaiting_handshake` or `error`. |
| *"probe failed: ... no such host"* | The URL you pasted doesn't resolve inside your cluster. Compare to `kubectl get svc -n monitoring`. |
| *"host not allowed"* | The proxy's allowed-hosts regex rejected the upstream. Likely the URL is a public hostname — switch to a `*.svc.cluster.local` form, or widen `edgeProxy.allowedHosts` in your helm install. |
| *"server error: HTTP 5xx"* | Prometheus itself is unhealthy. Daalu reached it; Prometheus didn't. |

### 5. Click "Connect Prometheus / Alertmanager"

The button at the bottom of the form saves it (on a row you're
editing it reads **Save changes**). The integration row turns
green; you can move on. From this
point on, every PromQL query the Assistant runs for your
tenant is routed through this cluster's edge proxy.

---

## Step 2 — Add Loki

Same shape. *Managed infra → Clusters & observability →
Observability → Loki (logs) → Connect.*

- **Reachable via federation tunnel** — same cluster as
  Prometheus (or a different cluster, if your logs live
  elsewhere).
- **Loki base URL** — `http://loki-gateway.monitoring.svc.cluster.local`
  is the most common shape (no port: 80). Some installs
  expose it as `:3100` — `kubectl get svc -n monitoring` will
  tell you which.
- **Username / Password** — only if your Loki is auth-gated.
  Most in-cluster installs aren't.

**Test connection** → **Connect Loki (logs)**. Done.

---

## Step 3 — Add Thanos (long-history metrics, optional)

If you have Thanos Query running alongside Prometheus, add it
the same way. The URL is whatever Service Thanos Query is
exposed on (usually `thanos-query.monitoring.svc.cluster.local:10902`).

The Assistant will use Thanos automatically for any time
range longer than your Prometheus retention window (we
recommend 7+ days).

If you don't have Thanos, skip this — Daalu degrades
gracefully to Prometheus-only.

---

## Step 4 — Add OpenSearch (optional)

If you index Kubernetes logs to OpenSearch (in addition to or
instead of Loki), wire it the same way.

- **OpenSearch base URL** — `https://opensearch-cluster-master.observability.svc.cluster.local:9200`
  is the most common shape from the OpenSearch operator.
  Note the `https` — OpenSearch ships with TLS on by default
  inside the cluster.
- **Username / Password** — typically `admin` and the value
  of your `OPENSEARCH_INITIAL_ADMIN_PASSWORD` secret, but
  the right thing to use is a **dedicated read-only user**
  scoped to your `k8s-logs-*` indices.

Click **Test connection.** OpenSearch's TLS cert won't have the
in-cluster DNS name in its SAN list, so the proxy may report
a TLS warning even on success — that's fine, the probe is
designed to accept it.

---

## Step 4b — Add Grafana (optional, unlocks Dashboards)

If you run Grafana, connect it the same way — URL (in-cluster
form through the tunnel, or public) plus an **API token** with
dashboard read access (a service-account token with the Viewer
role is enough for browsing; add Editor if you want Daalu's AI
to be able to create dashboards for you).

Connecting Grafana is what lights up the **Dashboards** page in
the sidebar: your Grafana boards, rendered natively inside
Daalu, with their panels filled from the Prometheus/Loki you
connected above. Chapter 48 is the walkthrough.

---

## What lights up once these are connected

Beyond the Assistant's ad-hoc queries:

- **Metric and log consoles.** Every connected Prometheus /
  Thanos / Loki row gets an **Open** link into a read-only
  console (`/observability/<provider>`) — an overview of the
  store's health (targets up/down, firing alerts, label
  inventory) plus a query runner with ready-made panels and a
  time window. All reads, no writes; it works through the
  tunnel like everything else. Chapter 17 covers it.
- **Alert ingestion, both directions.** Firing alerts flow in
  on a schedule — from Alertmanager when present, from
  Prometheus' own firing-alerts API when not — and when the
  upstream signal stops firing, Daalu **auto-resolves** the
  matching alert and its incident. Stale alerts no longer
  linger open forever.
- **Dashboards** (with Grafana connected) — Chapter 48.
- **GPU metrics** (if you connect a GPU) — the AI Factory page
  reads DCGM series from this same Prometheus. Chapter 50.

---

## Step 5 — Verify the Assistant can see your data

Open the **Assistant** in the right rail (or the **Assistant**
button on a phone), on Home or any page, and ask:

> "Show me the top 5 services with the highest error rate in
> the last hour."

Or, for logs:

> "Pull the last 50 lines from the openstack namespace
> containing the word 'error'."

If the path is working end-to-end:

- The Assistant runs the query via the new tunnel route.
- It cites the live query result in its answer.
- You can click the citation to see the exact PromQL or
  LogQL Daalu ran.

If you instead see *"I don't have access to that data"* or
*"no Prometheus configured for this tenant,"* go back to the
integration row — usually it's the cluster picker toggle that
wasn't ticked, so Daalu is trying to dial the
`*.svc.cluster.local` URL directly and failing silently.

---

## Path #1 — Publicly reachable observability (the shorter path)

If your observability stack is already exposed publicly
(e.g. `https://prometheus.acme.io` behind an SSO ingress),
the federation toggle is optional. You can leave it off, paste
the public URL, and Daalu will dial it directly from its own
infrastructure.

Trade-off: you need to grant Daalu's outbound IP access
through your auth proxy. Daalu's egress IPs are documented at
`https://docs.daalu.io/egress-ips` and are stable — you can
allow-list them. The federation-tunnel path doesn't need
this because nothing on your side opens an inbound port.

Most teams find the federation-tunnel path simpler in the
long run because it doesn't require coordinating allow-lists
with their security team.

---

## What's next

- **Connecting more clusters?** Repeat Step 0 for each.
  Every integration is then independently scoped to its own
  cluster.
- **Where do alert rules live?** In your Prometheus. Daalu
  pulls the alerts they fire every couple of minutes and closes
  them when they stop firing — chapter 20 (alerts and
  incidents) covers what you see.
- **Dashboards?** Chapter 48 shows your Grafana boards drawn
  natively inside Daalu; chapter 23 covers Reports.
- **Want a single deeper-dive on the catalog of observability
  integrations** (PagerDuty, Datadog adapters, etc.)?
  Chapter 31 in the integrations catalog is the reference.
