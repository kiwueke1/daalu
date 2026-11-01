---
id: metrics-app-high-error-rate
title: metrics-app error rate is high
match:
  alertname: [HighErrorRate]
  labels: {namespace: daalu-demo}
allowed_actions: [rollout_undo]
---
## Symptoms
- `HighErrorRate` fires: `rate(app_errors_total{job="metrics-app"}[2m]) > 0.5` for 1 minute.
- The pods are Running and Ready. This is a behaviour change, not a crash, so
  pod status and events look healthy.

## Diagnosis
1. Check what changed. `recent_changes` for namespace `daalu-demo` shows the
   newest metrics-app revision. The usual cause is the env change
   `metrics-app env ERROR_MODE: false → true`.
   ```
   kubectl -n daalu-demo rollout history deploy/metrics-app
   kubectl -n daalu-demo get deploy/metrics-app -o jsonpath='{.spec.template.spec.containers[0].env}'
   ```
2. Confirm in Prometheus that errors began with that rollout:
   ```
   rate(app_errors_total{job="metrics-app"}[2m])
   ```
   The error rate should be about 0 before the rollout and about 5/s after.

## Fix
1. Roll back to the previous revision (system step: `rollout_undo`), which
   has `ERROR_MODE=false`:
   ```
   kubectl -n daalu-demo rollout undo deploy/metrics-app
   ```
2. Verify the rollout finished:
   ```
   kubectl -n daalu-demo rollout status deploy/metrics-app --timeout=120s
   ```
   The 2-minute rate window has to drain, so the alert clears 2 to 3 minutes
   after the rollback.
