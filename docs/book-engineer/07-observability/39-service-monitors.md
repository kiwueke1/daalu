# 39. Service and Pod monitors

ServiceMonitor (SM) and PodMonitor (PM) are the
prometheus-operator CRDs that tell Prometheus what to scrape.
Our SMs live in `deploy/k8s/monitoring/03-servicemonitors.yaml`.

This chapter is a reference. If you're debugging "why isn't
this job in /targets?", read carefully.

---

## The catalog

Current SMs/PMs in `03-servicemonitors.yaml`:

| Resource | Type | Selects | Port |
|---|---|---|---|
| `daalu-api` | SM | Service `app: daalu-api` in daalu-automation/daalu | `http` (named) |
| `vllm-classifier` | SM | Service `app: llama-classifier` in daalu | `http` |
| `nvidia-dcgm` | SM | Service `app: nvidia-dcgm-exporter` in nvidia-device-plugin/gpu-operator | `gpu-metrics` |
| `daalu-workers` | PM | Pod `app: daalu-worker` in daalu-automation/daalu | `metrics` |
| `postgres-exporter` | SM | Service `app: postgres-exporter` in daalu-automation/daalu | `http` |

Each carries `release: kube-prom-stack` as a label (matching
the chart's release-name convention).

---

## How a ServiceMonitor maps to scrape targets

prometheus-operator turns a SM into Prometheus scrape config:

```yaml
- job_name: monitoring/daalu-api/0
  kubernetes_sd_configs:
    - role: endpoints
      namespaces:
        names: [daalu-automation, daalu]
  relabel_configs:
    - source_labels: [__meta_kubernetes_service_label_app]
      regex: daalu-api
      action: keep
    - source_labels: [__meta_kubernetes_endpoint_port_name]
      regex: http
      action: keep
```

The Service must have the matching label AND a port named
`http`. Both must be present. We've been bitten by this:
the daalu-api Service initially had unnamed ports and no
`app=daalu-api` label, so the SM matched nothing.

---

## The Service contract

For a SM to discover targets, the target Service must:

1. Have the label the SM matches (e.g., `app: daalu-api`).
2. Have a port named the SM expects (e.g., `http`).
3. Have at least one endpoint (backing pods).

The corresponding Helm template:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: daalu-api
  labels:
    {{- include "daalu.labels" . | nindent 4 }}
    app: daalu-api                  # required by ServiceMonitor selector
spec:
  selector:
    app: daalu-api
  ports:
    - name: http                    # required by ServiceMonitor port name
      port: 80
      targetPort: 8000
```

---

## PodMonitor contract

PodMonitor selects pods directly:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: daalu-workers
  namespace: monitoring
  labels:
    release: kube-prom-stack
spec:
  namespaceSelector:
    matchNames: [daalu-automation, daalu]
  selector:
    matchLabels:
      app: daalu-worker
  podMetricsEndpoints:
    - port: metrics
      interval: 30s
```

The pod must:

1. Have label `app: daalu-worker`.
2. Have a containerPort *named* `metrics`.
3. Expose `/metrics` on that port.

For workers, we run `prometheus_client.start_http_server(9100)`
in `workers/celery_app.py` and declare a containerPort
`metrics: 9100` in the Deployment spec.

---

## Debugging "job not in /targets"

A SM/PM that doesn't show up in Prometheus's targets page
fails one of:

1. The SM/PM has the wrong selector label (the Prometheus CR's
   `serviceMonitorSelector` doesn't pick it up). Our CR uses
   `{}` so this is rare.
2. The SM's selector doesn't match any Service. → Verify
   with `kubectl get svc -l <selector>`.
3. The Service's port name doesn't match the SM's `port`. →
   Verify with `kubectl get svc -o yaml`.
4. The Service has no endpoints (no backing pods, or all
   backing pods unhealthy). → Verify with `kubectl get
   endpoints`.

The Service Discovery page in the Prometheus UI shows
what was discovered before relabeling. Useful when you can
see "discovered 3 endpoints, kept 0" — usually a port-name
typo.

---

## A common failure mode

Service has the right label and port name, but the
NetworkPolicy doesn't allow Prometheus's namespace to reach
the target. Symptom: target shows up in `/targets` but with
status `down` and a "context deadline exceeded" error.

Fix: extend the NetworkPolicy on the target side. See the
network-policy template in the chart.

---

## Adding a new SM

Steps:

1. Verify the target service is set up correctly:
   - Labels match the planned SM selector.
   - Port is named.
   - `/metrics` is reachable.
2. Add the SM to `03-servicemonitors.yaml`.
3. Apply (or wait for ArgoCD sync).
4. Verify in Prometheus: `up{job="<your-job>"}` should
   return 1.

---

## What's next

Chapter 40 — dashboards and alert rules.
