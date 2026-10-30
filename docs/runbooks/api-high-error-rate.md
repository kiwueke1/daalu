# DaaluApiHighErrorRate

> Daalu API 5xx rate above 5% for 10 minutes.

## Why it matters

5xx means the server gave up. Customers see broken pages, the briefing
generator stops, agents lose state. Above 5% sustained is *the*
canonical "site is broken" signal.

## Investigate

```bash
# Which routes are erroring?
curl -s http://localhost:9090/api/v1/query?query='topk(5,sum by (handler,status) (rate(http_requests_total{job="daalu-api",status=~"5.."}[5m])))' | jq

# Recent error logs
kubectl -n daalu-automation logs -l app=daalu-api --tail=300 | grep -iE 'ERROR|exception'

# Is it one bad pod?
kubectl -n daalu-automation get pod -l app=daalu-api -o wide
```

Open the **Daalu — Overview** dashboard, panel "HTTP requests by
status" — visually confirms whether one pod / route is responsible.

## Resolve

**One pod is bad** — `kubectl delete pod <name>`; the Deployment
respawns and the bad one's traffic shifts to the healthy replicas.

**All pods are bad** — check the DB pool (panel on the Overview
dashboard). DB out of connections looks like 5xx on every write.
Restart Postgres if the pool is full; raise `db_pool_size` in
secrets if it's a sustained shape change.

**Recent deploy** — `git log --oneline -10` on master. If the spike
correlates with a SHA, roll the image tag back:
```bash
git checkout HEAD~1 -- deploy/k8s/api/deployment.yaml
git commit -m "rollback: revert api image"
git push origin master
```
ArgoCD picks up the change and the cluster reconciles.

## Escalate

If error rate stays >5% after 15 min of investigation, page the
on-call backend lead. If the route is `/api/v1/auth/*`, also page
security — broken auth is sometimes an attack.
