---
id: dummy-app-down
title: DummyApp is down
match:
  alertname: [DummyAppDown]
  labels: {namespace: demo}
allowed_actions: [rollout_undo, restart_deployment]
status: published
---
## Symptoms
`DummyAppDown` fires; `dummy-app` pods are not Ready, usually in
`CrashLoopBackOff` or `ImagePullBackOff`.

## Diagnosis
1. `recent_changes` for the `demo` namespace: was there a rollout in the last hour?
2. `rollout_history` for `deploy/dummy-app`: compare the image of the current
   and previous revisions.
3. `get_pod_logs` (previous container) for the crash message.

## Fix
- If the latest revision introduced the failure: `rollout_undo` to the previous revision.
- If the pods are wedged with no config change: `restart_deployment`.
- Verify: the deployment reports all replicas available and the alert resolves.
