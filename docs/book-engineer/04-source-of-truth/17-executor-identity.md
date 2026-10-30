# 17. Executor identity and the JWT scope gate

The executor is the only identity in Daalu allowed to mutate
external state on the customer's behalf. Multiple layers of
defense keep that property:

1. **Queue routing** — only the executor subscribes to the
   `executor` Celery queue.
2. **JWT scope gate** — write tools refuse to run without a
   JWT claim `scope: executor`.
3. **Kubernetes SA separation** — the executor pod runs as a
   different ServiceAccount with the credentials write tools
   need.

This chapter explains each layer.

---

## The queue boundary

Already covered in Chapter 7. The short version:

```python
# celery_app.py
task_routes = {
    "daalu.sot.execute_approved": {"queue": "executor"},
    "daalu.sot.reconcile":        {"queue": "default"},
    ...
}
```

The default worker pool subscribes to `default` and `agents`.
The executor subscribes to `executor`. A task routed to
`executor` *cannot* be picked up by the default workers; the
broker enforces this.

This is a strong physical barrier. Even if a bug in the API
mistakenly queues a write task as `default`, the default
workers don't import the executor's write tools, so it would
fail with an import error rather than execute.

---

## The JWT scope claim

The executor pod boots with a long-lived JWT in its
environment. The JWT carries an extra claim:

```json
{
  "sub": "executor",
  "tenant_id": null,
  "scope": "executor",
  "iat": 1779600000,
  "exp": 1811136000
}
```

`tenant_id: null` is a special "platform" identity, not
scoped to any single tenant. The scope is checked at
tool-call time.

The JWT is issued by a startup script and stored in the
executor's environment variable `DAALU_EXECUTOR_JWT`. The
chart's `templates/workers.yaml` mounts it from a Secret:

```yaml
env:
  - name: DAALU_EXECUTOR_JWT
    valueFrom:
      secretKeyRef:
        name: daalu-executor-jwt
        key: token
  - name: EXECUTOR_JWT_SCOPE
    value: "executor"     # pinned explicitly
```

The `EXECUTOR_JWT_SCOPE` env var is pinned even though it
matches the default — an ops-level secret rotation that
omits this key should be loud (the gate fails) rather than
silently re-bind the identity gate to whatever default is
hardcoded.

---

## The gate

`core/auth.py:_require_executor_scope`:

```python
EXECUTOR_SCOPE = os.environ.get("EXECUTOR_JWT_SCOPE", "executor")

def _require_executor_scope(payload: dict) -> None:
    if payload.get("scope") != EXECUTOR_SCOPE:
        raise PermissionError("executor scope required")
```

Called from every write tool:

```python
# core/kube_tools.py
async def _restart_pod(namespace, pod, cluster, ctx):
    payload = decode_token(ctx.token)
    _require_executor_scope(payload)
    ... # actually restart the pod
```

If a regular user's JWT somehow ended up driving this code
path (because of a bug), the gate refuses. The error
propagates up as a 500 with "executor scope required" in
the logs — easy to spot in observability.

---

## Kubernetes ServiceAccount

The executor pod also runs as a different SA from the worker
pool. Per `templates/workers.yaml`:

```yaml
serviceAccountName: {{ .Values.executor.serviceAccountName }}  # daalu-executor
automountServiceAccountToken: false
```

The `daalu-executor` SA gets cluster-write RBAC via
`templates/remediator-rbac.yaml`. It's the only SA in the
deployment that has those perms.

The worker pool's default SA (and the API pod's SA) can
only read.

So:

- API pod can read pods, propose actions.
- Worker pool can read pods, run agents, propose actions.
- Executor pod can read AND write — the only one that can
  call `restart_pod` and similar.

---

## Single replica

The executor chart pins replicas to 1:

```yaml
# Single replica on purpose — see workers/executor.py for
# the rationale. Adding pods would create additional
# executor identities racing on the same approved rows.
replicas: {{ .Values.executor.replicas }}    # 1
```

Multiple executor pods would race on `applying` transitions.
The pattern works but adds complexity (locking, dedup on the
broker). For our scale, one executor is enough; we scale by
bumping its `--concurrency` instead.

---

## What the executor must NEVER do

Failure modes that have been considered:

- **Approving its own proposals.** Not possible — the gate is
  on the API approve endpoint, not on the executor.
- **Running tasks from the default queue.** Not possible — it
  only subscribes to `executor`.
- **Skipping the four-eyes check.** Not possible — the check
  is at approve-time, not at execute-time. By the time the
  executor sees the proposal, it has already passed.
- **Mutating state without a proposal.** Possible in theory
  (the executor's JWT *could* call write tools directly), but
  not in practice — there is no code path that calls write
  tools outside of `run_plan(proposal.plan)`.

The defense-in-depth posture means each of these has both a
mechanism *and* a backup.

---

## What's next

Part V starts cluster federation. Chapter 24 — the
WireGuard hub.
