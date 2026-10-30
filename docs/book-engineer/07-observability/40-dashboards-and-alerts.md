# 40. Dashboards and alerts

Grafana dashboards and Prometheus alerting rules. Our
dashboards live under version control; alerts come from a
mix of CR-based rules and product-level rules.

---

## Dashboards

Stored in `deploy/k8s/monitoring/dashboards/` as JSON files,
mounted into Grafana via a ConfigMap.

The set we ship:

- **Daalu Platform Overview** — landing dashboard for the
  on-call. Pod health, queue depths, p50/p99 API latency,
  database connections, error rates.
- **LLM Router** — local vs cloud split, per-tier
  latencies, cost run-rate, queue depth.
- **GPU** — DCGM-exporter metrics for any GPU node in the
  observable scope. Utilization, VRAM, power, temperature.
- **Celery Workers** — task throughput, retries, failures,
  per-task duration.
- **Postgres** — connection pool, slow queries, replication
  lag (when we get replicas), bloat.
- **Cluster Federation** — tunnel handshakes, peer counts,
  through-tunnel error rates.

Each dashboard has a "Daalu" folder tag in Grafana so users
can find them easily.

---

## The native dashboards plane (Grafana-backed, rendered in-product)

Since PR #99 the product renders Grafana dashboards **natively** at
`/dashboards` — no iframe, no Grafana login. The division of labour:

> **Grafana is the catalog and the store. The tenant's own
> Prometheus/Thanos/Loki is the data plane.** Daalu reads dashboard *models*
> from Grafana and runs the panel queries itself against the tenant's
> stores.

The pieces:

- `core/grafana.py` — resolves a `grafana` Integration row (url + API
  token, + cluster-tunnel proxy), `list_dashboards`/`get_dashboard`/
  `create_dashboard`, and `translate_dashboard` (line 291): Grafana panel
  JSON → a flat Daalu render spec (one PromQL/LogQL per panel, panel kind
  mapped to timeseries/stat/table/logs).
- `core/tenant_metrics.py` — runs PromQL (instant + range) and LogQL
  against the tenant's own Prometheus/Thanos/Loki, mirroring the AI-factory
  GPU pattern (`core/prometheus.py:client_for_tenant`, chapter 80 §80.4).
- `api/routers/dashboards.py` — browse, get, per-panel `POST /query`, and
  `POST /ai` (**AI-create**): an LLM emits a Grafana dashboard model and
  Daalu POSTs it *back to Grafana* — Grafana stays the store, so the new
  dashboard lands in the same list as everything else.
- Frontend: `/dashboards` browser + AI-create modal,
  `/dashboards/[uid]` detail with native recharts panels and a range
  switcher; links from the managed-infra observability table and the
  cluster detail page.
- `grafana` is registered as an **adapterless provider** (token redacted on
  GET). Because it has no `IntegrationAdapter`, the integration-health beat
  originally reported "unknown provider: grafana" and pinned the badge to
  `error` forever while dashboards rendered fine — fixed with a dedicated
  `health()` probe (`GET /api/search`) wired as a `grafana` branch in the
  beat task (PR #102).

### The two resolution bugs that made panels render empty

Both bit the hub tenant first; both generalize.

1. **Which Prometheus?** (PRs #100/#101.) The first attempt preferred the
   tenant's `thanos` row — which on the hub points at a **store-less**
   thanos-query (0 series).
   The second attempt preferred any tunnel-bound row — but the hub tenant's
   rows are bound to the *workload* cluster tunnel, still the empty store.
   The durable rule (PR #101): **resolve the metric store co-located with
   the Grafana the dashboard came from** — a hub-local `grafana` row (no
   `cluster_tunnel_id`) uses the hub's `prometheus_base_url`; a tunnel-bound
   (customer) Grafana uses the prometheus/thanos row **on the same tunnel**
   via the edge proxy (prometheus preferred over thanos). A Grafana's panels
   query that Grafana's own datasource; follow the Grafana.
2. **Template variables** (PRs #100/#102). Grafana panel exprs carry
   `$job`/`$cluster`/`$namespace` and macros (`$__rate_interval`); sent raw,
   they match nothing. `core/tenant_metrics.py:expand_template_vars`
   (line 232) expands macros to sane defaults (5m/1h) and wildcard-expands
   var selectors — with one PromQL subtlety that blanked the entire
   kube-mixin Compute Resources family: an **exact** matcher containing a
   var (`namespace="$namespace"`) must be *promoted to a regex matcher*
   before wildcarding, becoming `namespace=~".*"` (matches all series *and
   absent labels*) — `namespace=".*"` is an exact match for the literal
   string `.*` and matches nothing. Likewise `!=` maps to `!~`, not the
   invalid `!=~`. This also fixes single-node clusters that carry no
   `cluster` label. Tests: `tests/test_dashboard_templating.py`.

### Editing dashboards

Two patterns:

1. **In Grafana directly** — clone the dashboard, edit,
   save under a new name. When happy, export the JSON and
   commit.
2. **Edit JSON in repo** — for small tweaks. Lower
   feedback loop but no risk of accidentally going off-
   piste.

The ConfigMap auto-reloads on change so committed JSON
deploys without a Grafana restart.

---

## Alert rules

We define alerts in three places:

### 1. PrometheusRule CRDs

In `deploy/k8s/monitoring/04-alerting-rules.yaml`. The kube-
prometheus-stack picks these up via the rule-selector.

Example:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: daalu-platform-alerts
  namespace: monitoring
  labels:
    release: kube-prom-stack
spec:
  groups:
    - name: daalu-api
      rules:
        - alert: DaaluApiDown
          expr: up{job="daalu-api"} == 0
          for: 5m
          labels:
            severity: critical
            team: platform
          annotations:
            summary: "daalu-api job has been down for 5 minutes"
            runbook_url: "https://docs.daalu.io/runbooks/api-down"
```

We have rule groups for: daalu-api, daalu-workers, postgres,
loki, federation, llm-router, ingest.

### 2. The Watchdog

A built-in always-firing rule:

```yaml
- alert: Watchdog
  expr: vector(1)
  labels:
    severity: none
  annotations:
    summary: "This is an alerting pipeline heartbeat."
```

Fires constantly. Routed to a low-volume channel. The
absence of beats means the alert pipeline is broken.

### 3. Application-level alerts

Some signals don't have Prometheus metrics — e.g., "a tenant
hasn't been seen in 24 hours." These are evaluated by
Daalu's own scheduled tasks (Celery beat) and fired via the
internal alert API rather than Prometheus.

---

## The tenant alert-ingest pipeline (June 2026 changes)

Three behaviours of the prometheus ingest adapter
(`modules/infra/integrations.py`) changed; all three matter for whether a
tenant's Alerts page reflects reality.

1. **Alertmanager-less clusters ingest too** (commit 5ad35a4). The adapter
   only polled Alertmanager's `/api/v2/alerts` — but the default GPU install
   ships kube-prometheus-stack with **Alertmanager disabled**, so a
   Prometheus-only cluster ingested zero events while showing "connected".
   When the Alertmanager API is absent (404/405) the adapter now falls back
   to Prometheus' own `/api/v1/alerts` (firing state,
   `integrations.py:294-309`), deriving a **stable fingerprint from the
   label set** so re-fires collapse onto one Alert.
2. **Alerts auto-resolve when the signal clears** (commit 89f42f0). The
   ingest tracks the active fingerprint set each cycle and emits
   `infra.alert.resolved` for any open/acknowledged alert *it previously
   opened* that is no longer firing (scoped via the source event so it only
   touches its own alerts). `InfraAgent._auto_resolve_alert`
   (`modules/infra/agent.py:168` — previously a stub) closes the matching
   alert + its incident by fingerprint. Before this, stale alerts lingered
   open forever.
3. **Alert-chat forces tool emission on remediation turns** (commit
   2da6409). `llm.chat_with_tools` / `_openai_compat_tools` accept and
   forward `tool_choice` (previously hard-coded `auto`), and alert-chat
   forces a tool call on the first investigative turn — re-prompting
   (bounded, with a nudge) when the model *narrates* a `[system]` fix
   without calling its tool — so the remediation lands as a pending,
   approvable `AlertAction` instead of prose that executes nothing.

One deployment war story attached to this pipeline: the ingest loop once
bound a variable named `annotations`, shadowing
`from __future__ import annotations`, and `ruff` (F402) held master's CI —
and therefore CD — red until it was renamed (commit 702d125). Lint failures
on this path block *deploys*, not just merges.

---

## Alertmanager routing

`alertmanager.yaml` (mounted via a Secret):

```yaml
route:
  receiver: daalu-default
  group_by: ['alertname', 'severity']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: [severity="none"]
      receiver: heartbeat
    - matchers: [severity="critical"]
      receiver: pagerduty-platform
    - matchers: [team="netops", severity!="info"]
      receiver: slack-netops
receivers:
  - name: daalu-default
    slack_configs:
      - api_url: <slack webhook>
        channel: '#ops-alerts'
  - name: pagerduty-platform
    pagerduty_configs:
      - routing_key: <pd routing key>
  - name: slack-netops
    slack_configs:
      - api_url: <slack webhook>
        channel: '#netops'
  - name: heartbeat
    slack_configs:
      - api_url: <slack webhook>
        channel: '#daalu-heartbeat'
```

Configured via the SOPS-encrypted `05-alertmanager-secrets.
yaml` in the repo.

---

## Customer-facing alerts vs platform-facing alerts

Two separate alert stacks:

- **Platform alerts** — fired against Daalu's own
  infrastructure. Routed to the Daalu engineering team's
  Slack + PagerDuty.
- **Tenant alerts** — fired *for* a specific customer's
  tenant. Routed via the tenant's configured notification
  channels (Chapter 29 of the customer book).

The two stacks share infrastructure (Prometheus,
Alertmanager) but use different routing rules.

---

## What's next

Part VIII covers deployment and CI/CD. Chapter 42 — local
development setup.
