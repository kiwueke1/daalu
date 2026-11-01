---
id: dummy-app-down
title: dummy-app is down (0 available replicas)
match:
  alertname: [DummyAppDown]
  labels: {namespace: daalu-demo}
allowed_actions: [rollout_undo]
---
## Symptoms
- `DummyAppDown` fires: `kube_deployment_status_replicas_available{deployment="dummy-app"} == 0` for 1 minute.
- dummy-app uses `strategy: Recreate`, so a bad rollout removes the old pod
  first and the service is fully down, not degraded.
- The new pod is in `ImagePullBackOff` / `ErrImagePull` (bad image tag) or
  `CrashLoopBackOff` (the container exits on startup).

## Diagnosis
1. Check what changed. `recent_changes` for namespace `daalu-demo` shows the
   newest dummy-app revision and what differs from the previous one (image
   or command).
   ```
   kubectl -n daalu-demo rollout history deploy/dummy-app
   ```
2. Confirm the pod state matches the change:
   ```
   kubectl -n daalu-demo get pods -l app=dummy-app
   kubectl -n daalu-demo describe pod -l app=dummy-app
   ```
   - Image change plus `Failed to pull image ... not found`: bad image tag.
   - Command change plus `CrashLoopBackOff` and logs ending in `crashing`:
     bad command override.
3. The previous revision should run `nginx:1.27-alpine` with no command
   override. That is the known-good spec.

## Fix
1. Roll back to the previous revision (system step: `rollout_undo`):
   ```
   kubectl -n daalu-demo rollout undo deploy/dummy-app
   ```
   The controller replaces the pod on its own. Don't delete pods afterwards.
2. Verify the rollout finished and the pod is Ready:
   ```
   kubectl -n daalu-demo rollout status deploy/dummy-app --timeout=120s
   ```
   The alert clears about a minute after available replicas return to 1.
3. If the rollback revision is also broken (the history has several bad
   revisions), roll back to the newest revision whose image is
   `nginx:1.27-alpine` with no command override:
   ```
   kubectl -n daalu-demo rollout undo deploy/dummy-app --to-revision=<N>
   ```
