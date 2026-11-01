---
id: demo-pod-crashlooping
title: A pod in the demo namespace is crash-looping
match:
  alertname: [KubePodCrashLooping]
  labels: {namespace: daalu-demo}
allowed_actions: [rollout_undo, restart_deployment]
---
## Symptoms
- `KubePodCrashLooping` (a kube-prometheus-stack default rule) fires for a pod
  in `daalu-demo` that has kept restarting for 15 minutes.
- In the demo this follows `break.sh crashloop`, which overrides dummy-app's
  container command so it exits after 2 seconds. `DummyAppDown` usually
  fired first. Handle both alerts with the same fix.

## Diagnosis
1. Find the owning Deployment from the pod name in the alert's `pod` label
   (`dummy-app-<hash>-<id>` belongs to `deploy/dummy-app`).
2. Read the crashed container's logs and its last termination reason:
   ```
   kubectl -n daalu-demo logs <pod> --previous
   kubectl -n daalu-demo describe pod <pod>
   ```
3. Check what changed (`recent_changes`, or `rollout history`). A new
   revision that adds a `command` or changes the image just before the
   restarts began is the cause.

## Fix
1. If a recent rollout introduced the crash, roll it back (system step:
   `rollout_undo`):
   ```
   kubectl -n daalu-demo rollout undo deploy/dummy-app
   ```
2. If nothing changed and the logs point at a transient dependency failure,
   a rolling restart is enough (system step: `restart_deployment`):
   ```
   kubectl -n daalu-demo rollout restart deploy/dummy-app
   ```
3. Verify:
   ```
   kubectl -n daalu-demo rollout status deploy/dummy-app --timeout=120s
   ```
