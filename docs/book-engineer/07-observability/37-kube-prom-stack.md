# 37. kube-prometheus-stack — values and customizations

`deploy/k8s/monitoring/01-kube-prometheus-stack-values.yaml`
is the Helm values for our kube-prometheus-stack release.
This chapter explains the choices that aren't defaults.

The Helm release is named `kube-prom-stack` — this matters
because the chart inserts the release name into resource
names (e.g., `kube-prom-stack-kube-prome-prometheus`). The
release name also acts as a selector label.

---

## Why kube-prometheus-stack

We picked it because:

- It bundles Prometheus + Alertmanager + Grafana + node-
  exporter + kube-state-metrics + prometheus-operator in one
  install.
- ServiceMonitor / PodMonitor CRDs are the lingua franca for
  Kubernetes scraping.
- Active community; predictable releases.

Alternatives considered:

- **OpenTelemetry Collector + Prometheus.** Forward-looking,
  but the stack is more moving parts.
- **VictoriaMetrics.** Higher density, better at long
  retention. We may switch when retention pressure grows.

---

## Retention and storage

The values pin Prometheus retention to 30 days and assign a
50 Gi PVC on `local-path` (k3s's single-node provisioner):

```yaml
prometheus:
  prometheusSpec:
    retention: 30d
    storageSpec:
      volumeClaimTemplate:
        spec:
          accessModes: ["ReadWriteOnce"]
          storageClassName: local-path
          resources:
            requests:
              storage: 50Gi
```

On a single-node k3s install `local-path` may be the only
storage class. Anything that asks for
a storage class that does not exist (e.g. a cloud CSI class) wedges Pending.

50 Gi is enough for our current cardinality at 30-day
retention. We watch utilization in Grafana; bump it when
we're at 70 %.

---

## additionalScrapeConfigs

The static scrape pattern for things that don't have a
ServiceMonitor:

```yaml
prometheus:
  prometheusSpec:
    additionalScrapeConfigs:
      - job_name: vllm
        kubernetes_sd_configs:
          - role: service
        relabel_configs:
          - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_scrape]
            action: keep
            regex: "true"
          - source_labels: [__meta_kubernetes_namespace]
            target_label: namespace
          - source_labels: [__meta_kubernetes_service_name]
            target_label: service
```

This is the "any service annotated with
`prometheus.io/scrape=true` gets scraped" pattern. We keep
one such job for vLLM and the dcgm-exporter.

This `additionalScrapeConfigs` block is rendered into a
Secret `kube-prom-stack-kube-prome-prometheus-scrape-confg`.
Drift between the repo and the deployed Secret is a thing
we've hit — when in doubt, sync the kube-prometheus-stack
ArgoCD Application.

---

## ServiceMonitor selectors

The Prometheus CR's `serviceMonitorSelector` and
`podMonitorSelector` are intentionally empty:

```yaml
prometheus:
  prometheusSpec:
    serviceMonitorSelector: {}
    podMonitorSelector: {}
    serviceMonitorNamespaceSelector: {}
    podMonitorNamespaceSelector: {}
    serviceMonitorSelectorNilUsesHelmValues: false
    podMonitorSelectorNilUsesHelmValues: false
```

`{}` means "match all". The reason: we want any
ServiceMonitor in any namespace to be discoverable, so adding
a new SM doesn't require a label dance.

The downside is that an SM with a bad selector still gets
picked up — but the worst that happens is the job appears in
`/targets` with no instances. That's much easier to debug
than "the SM isn't being picked up at all."

---

## Grafana

The Grafana subchart is enabled with:

```yaml
grafana:
  adminPassword: <set via existing secret>
  service:
    type: ClusterIP
  ingress:
    enabled: true
    ingressClassName: traefik
    hosts:
      - grafana.example.com
    tls:
      - secretName: grafana-daalu-io-tls
        hosts: [grafana.example.com]
  persistence:
    enabled: true
    storageClassName: local-path
    size: 5Gi
  dashboardProviders:
    dashboardproviders.yaml:
      apiVersion: 1
      providers:
      - name: 'daalu'
        type: file
        folder: 'Daalu'
        path: /var/lib/grafana/dashboards/daalu
  dashboardsConfigMaps:
    daalu: daalu-dashboards
```

Our dashboards are committed in `deploy/k8s/monitoring/
dashboards/` and loaded via the ConfigMap.

---

## Alertmanager

```yaml
alertmanager:
  alertmanagerSpec:
    configSecret: kube-prom-stack-kube-prome-alertmanager
  ingress:
    enabled: false   # no public exposure; port-forward for UI
```

The configSecret points at a secret we manage out-of-band
(it has the Slack webhook + PagerDuty routing key). See
Chapter 47 on SOPS.

---

## What pods exist after install

After the chart is installed, the `monitoring` namespace has:

```
daemonset.apps/kube-prom-stack-prometheus-node-exporter
deployment.apps/kube-prom-stack-grafana
deployment.apps/kube-prom-stack-kube-prome-operator
deployment.apps/kube-prom-stack-kube-state-metrics
statefulset.apps/alertmanager-kube-prom-stack-kube-prome-alertmanager
statefulset.apps/prometheus-kube-prom-stack-kube-prome-prometheus
```

Note the `kube-prome-` infix — this is the chart's
`<release>-<chartShortName>-<component>` pattern. We've
been bitten by typo-ing this in docs; if a Service name
doesn't resolve, check whether it's missing the `kube-prome-`
piece.

---

## What's next

Chapter 38 — Loki and Promtail.
