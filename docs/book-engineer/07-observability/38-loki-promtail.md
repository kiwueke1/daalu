# 38. Loki and Promtail

Logs are collected by Promtail (a per-pod daemon) and stored
in Loki. Both deploy from their own Helm charts in the
`monitoring` namespace.

---

## Loki

`deploy/k8s/monitoring/02-loki-values.yaml`. We run in
**monolithic mode** (a single StatefulSet) because the
volume doesn't yet justify the operational cost of
microservices mode.

```yaml
deploymentMode: SingleBinary
loki:
  auth_enabled: false        # internal, behind NetworkPolicy
  commonConfig:
    replication_factor: 1
  storage:
    type: filesystem
    filesystem:
      chunks_directory: /var/loki/chunks
      rules_directory: /var/loki/rules
  schemaConfig:
    configs:
      - from: "2026-01-01"
        store: tsdb
        object_store: filesystem
        schema: v13
singleBinary:
  replicas: 1
  persistence:
    enabled: true
    storageClass: local-path
    size: 100Gi
```

Retention: 14 days, configured in `limits_config`:

```yaml
loki:
  limits_config:
    retention_period: 14d
```

A compactor job runs daily to enforce.

---

## Promtail

`deploy/k8s/monitoring/02-loki-values.yaml` (same file) plus
the `promtail` Helm release.

Promtail runs as a DaemonSet — one pod per node. It tails
`/var/log/pods/*/*.log` and ships to Loki via push.

```yaml
config:
  clients:
    - url: http://loki.monitoring.svc:3100/loki/api/v1/push
  positions:
    filename: /run/promtail/positions.yaml
  scrape_configs:
    - job_name: kubernetes-pods
      kubernetes_sd_configs:
        - role: pod
      relabel_configs:
        - source_labels: [__meta_kubernetes_pod_label_app]
          target_label: app
        - source_labels: [__meta_kubernetes_namespace]
          target_label: namespace
        - source_labels: [__meta_kubernetes_pod_name]
          target_label: pod
        - source_labels: [__meta_kubernetes_pod_container_name]
          target_label: container
        # Drop kube-system noise except for stuff we care about
        - source_labels: [__meta_kubernetes_namespace]
          regex: kube-system
          action: drop
```

Labels we use:

- `app` — the pod's `app` label.
- `namespace`, `pod`, `container` — standard.
- `tenant` — when the pod is a daalu-api pod (set via
  `__meta_kubernetes_pod_label_tenant`).

We deliberately keep the label cardinality small. Loki's
cardinality is much more sensitive to high-cardinality
labels than Prometheus.

---

## Querying

The Assistant uses Loki for log context:

```python
async def query_loki(query: str, time_range: tuple[datetime, datetime]) -> list[str]:
    r = await httpx_get(
        "http://loki.monitoring.svc:3100/loki/api/v1/query_range",
        params={"query": query, "start": ..., "end": ..., "limit": 1000},
    )
    return r.json()["data"]["result"]
```

LogQL queries we use heavily:

- `{app="daalu-api"} |= "error"` — recent API errors.
- `{namespace="daalu-automation"} |= "panic"` — panics.
- `{app="vllm-classifier"} | json | duration_ms > 1000` —
  slow inference.

---

## Retention strategy

- Pod logs: 14 days in Loki.
- Cluster events (kube events): forwarded to Loki via
  `eventrouter`, same retention.
- Audit logs (DB-side): per the plan's retention window
  (365 days default).

Logs are heavy — 200+ GB/day at peak. Compression + chunking
in Loki keeps it manageable.

---

## Promtail vs Vector vs Fluent Bit

We picked Promtail because:

- It ships with the Loki ecosystem.
- It auto-discovers Kubernetes pods.
- It handles the position-file persistence we need.

Vector is faster and more general-purpose. We may revisit
when we need transformations Promtail can't do.

---

## What's next

Chapter 39 — ServiceMonitors and PodMonitors.
