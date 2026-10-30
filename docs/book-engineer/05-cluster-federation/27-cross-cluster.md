# 27. Cross-cluster API access and scraping

Once the WireGuard tunnel is up, the operator app can call
services inside the customer's cluster as if they were local
addresses. This chapter covers the patterns we use.

---

## Three categories of cross-cluster call

1. **Kubernetes API** — kubectl-equivalent calls.
2. **HTTP service calls** — Prometheus queries, vLLM
   inference, Loki queries.
3. **Prometheus scraping** — Prometheus on the operator side
   scraping in-cluster targets through the tunnel.

Each is slightly different.

---

## Kubernetes API

The edge pod has a ServiceAccount with RBAC. Inside the edge
pod we run a small **kube proxy**:

```
[ operator app ]  →  http://10.200.0.42:8001/api/...  →  [ edge ]  →  in-cluster kube-apiserver
```

The proxy is the standard `kubectl proxy` running as a
container in the edge pod (or via the ambassador pattern).
It impersonates the edge SA's token.

From the operator app side, we have a thin client in
`core/cross_cluster/kube.py`:

```python
def cluster_client(cluster: ClusterTunnel) -> ApiClient:
    cfg = Configuration()
    cfg.host = f"http://{cluster.tunnel_ip}:8001"
    cfg.verify_ssl = False
    return ApiClient(configuration=cfg)
```

The operator app's kube tools (`core/kube_tools.py`) use this
client; they don't need to know the kubeconfig of the
customer's cluster.

---

## HTTP service calls

For Prometheus / Loki / vLLM, we hit the in-cluster service
DNS through the tunnel:

```python
endpoint = f"http://{cluster.tunnel_ip}:9100/proxy/<in-cluster-target>:9090"
```

The edge pod runs a simple HTTP proxy on port 9100 that
forwards `/proxy/<host>:<port>/<path>` to the in-cluster
target. This is what makes "Prometheus inside your cluster
is queryable" work.

The proxy has tight URL allowlist enforcement — only the
Daalu-recognized service names can be reached.

---

## Prometheus scraping through the tunnel

Some customers want their in-cluster Prometheus to be a
**scrape target** for Daalu's Prometheus (so Daalu's
operator-side dashboards can show data from their cluster).

The pattern:

- The operator-side Prometheus has an `additionalScrapeConfig`
  with a static target like `<tunnel-ip>:9100`.
- The edge pod's proxy forwards scrape requests to the
  in-cluster Prometheus's `/api/v1/query`-like endpoints.

This isn't the primary way Daalu uses customer metrics —
the primary way is on-demand queries via the Assistant. The
scrape pattern is for built-in dashboards that aggregate
across all federated clusters.

---

## Latency

Every cross-cluster call adds:

- WireGuard encryption: ~1 ms.
- Network from operator to customer: 50–200 ms typical.
- WireGuard decryption: ~1 ms.
- In-cluster routing: <1 ms.
- The target service's own processing.

For Prometheus queries, the network leg dominates. We cache
aggressively where possible.

---

## Failure handling

When a cross-cluster call fails:

- **Tunnel down (handshake stale > 5 min):** caller sees a
  connection-refused error. Operator app routes around it
  if it can (e.g., the Assistant falls back to a different
  data source); otherwise the operator user sees a clear
  "cluster X unreachable" message.
- **Tunnel up, target service down:** caller sees the
  target's error. E.g., Prometheus returns 503 →
  surfaced as-is.
- **Tunnel up, target service slow:** the API enforces a
  per-call timeout (default 10s). Slow scrapes get killed
  rather than dragging the request handler.

---

## Security model

A few principles:

- The hub never initiates inbound to the customer's network.
  The customer's edge pod always initiates the tunnel.
- The tunnel IPs are private (`10.200.x.x`) and not routed
  outside the WireGuard mesh.
- The edge pod's RBAC determines what's reachable inside the
  cluster.
- Every cross-cluster call is logged on the operator side
  (caller user, target URL, status, duration).

Chapter 57 has the formal isolation contract.

---

## What's next

Part VI starts the GPU/LLM stack. Chapter 30 — vLLM
deployment on the workload cluster.
