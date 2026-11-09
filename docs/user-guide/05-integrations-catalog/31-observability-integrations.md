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

Connected Prometheus / Thanos / Loki rows additionally get an
**Open** link into a read-only metric/log console — overview
plus a query runner — described in Chapter 17.

---

## Setup pattern

For each, *Managed infra → Integrations → \[type\] →
Configure*. The wizard asks for:

- **Base URL** — `https://prom.acme.io`, `http://loki.svc:3100`,
  etc.
- **Auth method** — basic auth, bearer token, or none.
- **Reachable via federation tunnel?** — tick if it's inside
  your network.
- **Friendly name.**

Daalu does a test query (`up{} == 1` for Prometheus, a benign
LogQL for Loki, etc.). If it succeeds, the row goes green.

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

For Prometheus and Grafana, Daalu auto-imports firing alerts:

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
  gone. Acknowledging or resolving in Daalu is mirrored back to
  the upstream system where possible.

For Loki, you'd configure alert rules in your Loki ruler; same
mirroring story.

For OpenSearch, alerting plugins vary; Daalu pulls
alarms-as-alerts where the plugin's API supports it.

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

## Choosing one when you have many

If your tenant has multiple Prometheuses (per-cluster), Daalu
needs to route queries to the right one. This is configured
per-integration:

- **Default destination** — the one the Assistant uses for
  unscoped queries.
- **Scoped routes** — labels that force routing to a specific
  Prometheus.

Example: any query with `cluster: eu-prod` goes to the eu-prod
Prometheus; any query with `cluster: us-east` goes to the
us-east Prometheus; everything else goes to the default.

---

## Capacity and rate limits

Daalu's observability adapters have per-target rate limits:

- Prometheus: ~30 queries/minute by default.
- Loki: ~10 queries/minute (log queries are expensive).
- OpenSearch: ~30 queries/minute.
- Grafana: ~30 API calls/minute.

These can be raised in *Settings → Observability → Rate limits*
if your stack handles more. Be conservative — DOS-ing your own
metrics is an unhappy event.

---

## Troubleshooting

Common issues:

- **Connection refused** — the URL is wrong, or the service
  isn't running.
- **401/403** — auth method wrong.
- **No data returned for a query that should work** — the
  Daalu side stripped the wrong label, or the rate limit
  fired. Check the integration detail page's "Last query"
  trace.
- **Stale alert mirror** — the import schedule is 15 minutes
  by default; for faster updates, configure your Prometheus
  to webhook alerts directly to Daalu's webhook endpoint.

---

## What's next

Chapter 32 covers CRMs and business systems.
