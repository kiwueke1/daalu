# 31. Observability integrations (Prometheus, Grafana, Loki, Thanos, OpenSearch)

The observability integrations are how Daalu reaches your
metrics, logs, and traces. The Assistant can run real queries
against them; alert rules can be mirrored from them.

This chapter covers all five together because the setup is
similar.

---

## What each integration gives you

- **Prometheus** — Daalu can run PromQL queries on demand and
  pull alert rules.
- **Thanos** — same surface as Prometheus, but with long-
  retention queries.
- **Loki** — Daalu can run LogQL queries; the alert-explainer
  agent uses this for log context.
- **OpenSearch / Elasticsearch** — log search via OpenSearch
  DSL.
- **Grafana** — Daalu reads your dashboard catalog and renders
  the boards **natively** on the Dashboards page (Chapter 48),
  filling panels from your own Prometheus/Loki; the AI can also
  write new dashboards back into Grafana for you.

Connected Prometheus / Thanos / Loki / OpenSearch rows
additionally get an **Open** link (Grafana's opens the
Dashboards page) into a read-only metric/log console — overview
plus a query runner — described in Chapter 17.

---

## Setup pattern

For each, *Managed infra → Clusters & observability →
Observability → \[type\] → **Connect*** (**Edit** once it's
connected). You can also set everything up in order on the
*Onboarding* tab. The dialog asks for:

- **Base URL** — `https://alertmanager.acme.io`,
  `http://loki.svc:3100`, etc. (for Prometheus, Daalu asks for
  the Alertmanager v2 base URL).
- **Username** / **Password** — optional, for Loki and
  OpenSearch; Grafana takes a service-account **API token**.
- **Reachable via federation tunnel** — tick if it's inside
  your network, and pick the cluster.

Daalu does a test query (`up{} == 1` for Prometheus, a benign
LogQL for Loki, etc.) when you click **Test connection**. Save
with **Connect \[type\]**. To remove one, use the delete icon on
its row, which asks you to confirm in place.

---

## Federation-tunnel observability

If your Prometheus lives inside a federated cluster (the common
case for on-prem operators):

1. Connect the cluster first (Chapter 14).
2. When adding the observability integration, tick "Reachable
   via federation tunnel" and use the cluster-internal URL
   (`http://kube-prom-stack-kube-prome-prometheus.monitoring.svc:9090`).
3. Daalu routes the query through the tunnel.

This is the standard pattern.

---

## Alert rules

For Prometheus / Alertmanager, Daalu auto-imports firing
alerts:

- Pulls them on a schedule.
- When an upstream rule fires, the Daalu adapter sees the
  alert and writes a Daalu alert. Re-fires of the same rule
  collapse onto the same alert rather than duplicating.
- **No Alertmanager? Still works.** If your Prometheus runs
  without Alertmanager (a common default), Daalu automatically
  reads Prometheus' own firing-alerts API instead — point the
  integration at the Prometheus *server* URL and the alert
  pipeline fills either way.
- **Alerts auto-resolve.** When the upstream signal stops
  firing, Daalu closes the matching alert and its incident on
  the next cycle — nothing lingers open after the problem is
  gone.

Loki, Thanos, OpenSearch and Grafana aren't alert sources:
the agent queries them while it investigates. To alert on logs,
route your Loki ruler's alerts through Alertmanager, and Daalu
picks them up from there.

---

## What the Assistant uses each for

When the Assistant investigates an alert:

- **Prometheus** — pulls the metric value at the time of fire
  and N minutes around.
- **Loki** — pulls logs from the relevant labels at the time
  window.
- **OpenSearch** — searches indices the alert's source maps
  to.
- **Grafana** — reads dashboards to identify the panel that
  represents the metric.
- **Thanos** — long-context queries (e.g., "is this metric
  trending up over the last 30 days?").

You can see what the Assistant queried from any answer it
gave — citations link to the live query results.

---

## One of each per organisation

Each observability provider holds one connection per
organisation: one Prometheus / Alertmanager, one Loki, and so on.
Connecting again replaces the existing one (use **Edit**). If you
run several Prometheuses, point Daalu at the one that federates
or aggregates the others, or at a Thanos Query in front of them.

---

## Troubleshooting

Common issues:

- **Connection refused** — the URL is wrong, or the service
  isn't running.
- **401/403** — the username, password or API token is wrong.
- **No data returned for a query that should work** — open the
  provider from its row (**Open**) and run the same query in
  the console to see what the source returns.
- **Stale alert mirror** — Daalu checks Alertmanager for firing
  alerts every couple of minutes. For an immediate check, click
  **Check now** on the Prometheus card under *Managed infra →
  Integrations → Ingest sources*.

---

## What's next

Chapter 32 covers CRMs and business systems.
