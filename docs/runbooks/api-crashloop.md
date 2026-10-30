# DaaluApiPodCrashLooping

> A daalu-api pod has restarted in the last 10 minutes.

```bash
kubectl -n daalu-automation describe pod <pod>     # exit reason
kubectl -n daalu-automation logs <pod> --previous  # what it said before dying
```

Most common causes: OOM (raise `resources.limits.memory`), missing
secret (the init container fails on alembic; check secret keys),
unreachable Postgres (check `kubectl get svc/postgres`).
