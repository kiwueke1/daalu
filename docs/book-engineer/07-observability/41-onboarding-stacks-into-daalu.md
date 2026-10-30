# 41. Onboarding observability stacks into Daalu

Chapters 37-40 cover the kube-prometheus-stack we run *for*
Daalu's own monitoring. This chapter is the other direction:
how an engineer or operator wires an *external* observability
stack — the tenant's Prometheus, Loki, Thanos, OpenSearch —
*into* Daalu so the agent can query it.

There are two cases to keep separate, because the URL shapes
that work differ between them:

| Case | URL shape that works | Mechanism |
|---|---|---|
| The stack is publicly reachable (or on the same operator cluster as `daalu-api`) | `https://prom.acme.io` / `http://kube-prom-stack-…svc:9090` | Direct dial. No federation needed. |
| The stack lives in a customer-managed workload cluster (the common on-prem case) | `http://prometheus.monitoring.svc.cluster.local:9090` | Routed through that cluster's daalu-edge HTTP proxy via the WireGuard tunnel. **Cluster must be onboarded first.** |

The second case is what most tenants actually need — operators
don't want to expose Loki/Prometheus on the public internet.
This chapter focuses on it.

---

## The pieces involved

1. **`ClusterTunnel`** row — per (tenant, cluster) WireGuard
   peer. Created by *Managed infra → Kubernetes clusters → Onboard
   cluster*. Stores the operator/customer pubkeys, the
   `tunnel_ip` allocated on `10.200.0.0/16`, and the live status.
   See `src/daalu_automation/models/cluster_tunnel.py`.

2. **`daalu-edge`** pod in the workload cluster. Now ships with
   **two routing sidecars by default**:
   - `apiserver-proxy` — socat at `<tunnel_ip>:6443` →
     `kubernetes.default.svc:443`. Unchanged from before.
   - `edge-proxy` *(new)* — Python aiohttp HTTP forward-proxy at
     `<tunnel_ip>:8888`. Source: `deploy/edge/proxy.py`. Lets the
     hub dial *any* in-cluster URL by setting it as
     `httpx`'s `proxy=...`. Helm value: `edgeProxy.enabled=true`
     (default on).

3. **`Integration.cluster_tunnel_id`** column (added in
   migration `0016_integration_cluster_tunnel`). When set, the
   integration's URL is dialed through that tunnel's edge proxy
   instead of directly. NULL = legacy behavior (direct dial).

4. **`core/cluster_proxy.get_proxy_url(db, cluster_tunnel_id)`** —
   the single lookup helper. Returns
   `http://<tunnel_ip>:8888` or `None`. Every httpx call site
   that dials an integration URL (`_query_prometheus`,
   `_query_loki`, `_test_loki`/`_test_prometheus`/`_test_thanos`/
   `_test_opensearch`, `PrometheusAdapter.ingest`,
   `_http_probe`) takes a `proxy` arg and forwards it to
   `httpx.AsyncClient(proxy=...)`.

5. **`POST /onboarding/test/{provider}`** and
   **`PUT /integrations/config/{provider}`** both accept a
   top-level `cluster_tunnel_id` (sibling of `config`). The PUT
   handler rejects cluster IDs that don't belong to the calling
   tenant.

---

## Provider field reference

The backend's required-field list is at
`src/daalu_automation/api/routers/onboarding.py:70`; the wizard
mirror is at `frontend/components/integrations/steps.tsx:120`.
All four observability providers have `supportsCluster: true`
on their step definition, which is what makes the "Reachable via
federation tunnel" toggle appear in the wizard / connect modal.

| Provider | Required | Optional | Test probe | Path |
|---|---|---|---|---|
| `prometheus` | `url` (Alertmanager v2 base) | — | `GET /-/healthy` | `_test_prometheus` |
| `loki` | `url` | `user`, `password` | `GET /loki/api/v1/labels` | `_test_loki` |
| `thanos` | `url` | — | `GET /-/healthy` | `_test_thanos` |
| `opensearch` | `url` | `user`, `password` | `GET /` (any < 500 = reachable) | `_test_opensearch` |

When dialed through the proxy, the probe URL stays the same;
only the `httpx.AsyncClient(proxy=...)` differs.

---

## The end-to-end data flow (one read)

Worked example: the alert-chat agent runs a LogQL query.

```
 daalu-api pod (operator cluster)
        │
        │  1. _query_loki(query, _tenant_id)
        │     │  • SELECT integrations WHERE tenant_id=? AND provider='loki'
        │     │  • cluster_tunnel_id = ct-uuid
        │     │  • get_proxy_url(db, ct-uuid) → "http://10.200.0.2:8888"
        │     │
        │  2. httpx.AsyncClient(proxy="http://10.200.0.2:8888")
        │       .get("http://loki-gateway.monitoring.svc.cluster.local/loki/api/v1/query_range", ...)
        ▼
 daalu-wireguard-hub  ──── encrypted UDP ────▶  daalu-edge pod (workload cluster)
                                                  │  3. edge-proxy listens on 10.200.0.2:8888
                                                  │  4. Sees absolute-form GET; resolves
                                                  │     loki-gateway.monitoring.svc.cluster.local
                                                  │     via in-cluster kube-dns
                                                  ▼
                                              loki-gateway:80
                                                  │
                                                  │  5. Response streams back through
                                                  ▼     proxy → tunnel → daalu-api
```

The customer never opens a port to the public internet for any
of this. The only inbound traffic to the workload cluster is the
WireGuard UDP listener (and even that is often initiated outbound
via `PersistentKeepalive = 25`, which is why most installs work
behind CGNAT).

---

## Onboarding it — UI (the common path)

Prereq: a **connected** ClusterTunnel for the target cluster.
The wizard's cluster picker is disabled until at least one
cluster reaches `status = connected`.

1. **Onboard the cluster** (if you haven't): *Managed infra →
   Kubernetes clusters → Onboard cluster*. Copy the helm-install
   snippet and run it in the workload cluster. Wait for the row's
   status pill to flip to **connected**.

2. **Open the observability step** in the wizard (*Onboarding →
   Prometheus*) or click **Configure** on the card on
   *Integrations*.

3. **Tick "Reachable via federation tunnel"** — the picker
   appears (`frontend/components/integrations/steps.tsx`,
   the `ClusterPicker` component). Pick the cluster.

4. **Paste the in-cluster URL** —
   `http://prometheus.monitoring.svc.cluster.local:9090` or
   whatever the customer's Service name is.

5. **Test** → the wizard POSTs
   `/onboarding/test/prometheus` with
   `{"config":{"url":"…"},"cluster_tunnel_id":"…"}`. Backend
   resolves the proxy and runs the probe through it. Green
   means kube-dns inside the workload cluster resolved the host
   and the upstream answered.

6. **Save** → `PUT /integrations/config/prometheus` persists
   the `cluster_tunnel_id` on the row. From here on, every
   `_query_prometheus` for this tenant is routed through that
   cluster's edge.

Same flow for Loki, Thanos, OpenSearch.

---

## Onboarding it — API (scripted)

The wizard is the primary UI, but everything works from a PAT.

```bash
export DAALU_API="https://api.example.com"
export DAALU_TOKEN="dak_..."   # tenant-admin PAT

# 1. Find the ClusterTunnel id you want to attach to.
curl -s -H "Authorization: Bearer $DAALU_TOKEN" \
  "$DAALU_API/clusters" \
  | jq '[.[] | select(.status=="connected") | {id, slug, tunnel_ip}]'
# → [{"id":"…uuid…","slug":"daalu-workload","tunnel_ip":"10.200.0.2"}]

CT_ID="…uuid…"

# 2. Probe the URL through that tunnel. The body's cluster_tunnel_id
#    is what makes the test go through the edge proxy.
curl -s -X POST -H "Authorization: Bearer $DAALU_TOKEN" \
  -H "Content-Type: application/json" \
  "$DAALU_API/onboarding/test/loki" \
  -d "$(jq -nc --arg ct "$CT_ID" '{
        config:{url:"http://loki-gateway.monitoring.svc.cluster.local:80"},
        cluster_tunnel_id:$ct
      }')"
# → {"ok":true,"message":"endpoint responded","latency_ms":47}

# 3. Persist the row with the same cluster_tunnel_id.
curl -s -X PUT -H "Authorization: Bearer $DAALU_TOKEN" \
  -H "Content-Type: application/json" \
  "$DAALU_API/integrations/config/loki" \
  -d "$(jq -nc --arg ct "$CT_ID" '{
        config:{url:"http://loki-gateway.monitoring.svc.cluster.local:80"},
        cluster_tunnel_id:$ct
      }')" | jq
```

To **move** an integration to a different cluster, PUT with
the new `cluster_tunnel_id`. To **detach** (back to direct
dial), PUT with `cluster_tunnel_id: null`. To **leave it alone**,
omit the field — the backend uses Pydantic's
`model_fields_set` to distinguish "explicitly null" from
"not provided" (see
`src/daalu_automation/api/routers/integrations.py`).

---

## Verifying it works end-to-end

After the row is saved, three places will tell you if the path
is live:

```bash
# 1. From a daalu-api pod, poke the proxy directly.
kubectl --kubeconfig "$KUBECONFIG_OPS" -n daalu-automation \
  exec deploy/daalu-api -c api -- \
  curl -fsS -x http://10.200.0.2:8888 \
       http://loki-gateway.monitoring.svc.cluster.local/loki/api/v1/labels \
  | jq '.status'
# Expected: "success"

# 2. From the edge pod, watch the proxy's structured logs while
#    you click "Test" in the UI.
kubectl --kubeconfig "$KUBECONFIG_WL" -n daalu-edge \
  logs deploy/daalu-edge -c edge-proxy --tail=20
# Expected: one log line per probe, no "rejected … not in ALLOWED_HOSTS"

# 3. From the agent's perspective, ask a copilot question that
#    triggers _query_loki.
```

---

## What does *not* yet flow through the proxy

A few call sites still dial integration URLs directly. They're
documented here so the next person to touch them knows what's
left:

- **Cloud provider adapters** (`aws`, `gcp`, `azure`) — these
  hit public cloud endpoints, not in-cluster URLs. Setting
  `cluster_tunnel_id` on them is currently a no-op; the column
  exists on the row for future use (e.g., dialing a private
  VPC endpoint through a customer-side egress proxy).
- **`_call_external_api`** — the generic tool the LLM uses to
  hit arbitrary registered HTTP integrations. Also a candidate
  for the proxy plumbing; same pattern, no code yet.

Two former entries on this list are now **done**: **Nautobot** — the SoT
adapter resolves `get_proxy_url` from the chosen integration row and dials
the NV-CM environment's gateway over the tunnel (chapter 15); and
**SSH** (non-HTTP) — the LLDP probe, device terminal and `linux_ssh`
adapter splice raw TCP through the edge's HTTP `CONNECT` support
(`core/cluster_proxy.py:open_connect_socket`, chapter 79).

---

## Failure modes specific to the cross-cluster path

| Symptom | Likely cause | Fix |
|---|---|---|
| Test returns `probe failed: All connection attempts failed` after toggling federation on | Tunnel isn't actually connected — the picker only filters by row presence + `connected` status; if the row is `awaiting_handshake` it will fail | Check *Managed infra → Kubernetes clusters*; re-onboard if necessary |
| Test passes, but `Run ingest` emits 0 events forever | Adapter URL is correct but Alertmanager has no firing alerts — not a routing problem | Confirm by hitting the URL with `kubectl exec daalu-api -- curl -x http://<tunnel_ip>:8888 …/api/v2/alerts` |
| Proxy logs show `rejected … not in ALLOWED_HOSTS` | The default regex only allows `*.svc[.cluster.local]` and RFC1918 — a customer pointing at a hostname like `loki.acme.internal` will be blocked | Set `edgeProxy.allowedHosts` in the workload cluster's helm values to a wider regex, then `helm upgrade` |
| Wizard cluster picker is empty | No row has `status='connected'` for this tenant | Onboard or repair a cluster first |
| `PUT /integrations/config/...` returns 400 `cluster_tunnel_id … not found for this tenant` | The UUID belongs to a different tenant — the handler refuses cross-tenant attachment as a safety check | Use the cluster ID from `GET /clusters` (already tenant-scoped) |
| Metric console shows "unreachable / HTTP 404" on every panel | The row was registered with the **Alertmanager** URL (`:9093`, no `/api/v1/query`) instead of the Prometheus server (`prometheus-operated:9090`) | Repoint the row; the server URL backs both the console and alert ingest (chapter 78) |
| Store queries through the tunnel truncate / `JSONDecodeError` / stall | gzip'd response + edge proxy = wrong `Content-Length` | Send `Accept-Encoding: identity` (already done in `core/observability_console.py:_headers`; do the same in new call sites) |
| Green connection Test but zero data on GPU/dashboard panels | Wrong Service name pasted (each kube-prometheus-stack release names its Service differently — e.g. `kube-prometheus-stack-prometheus` vs `kube-prom-stack-kube-prome-prometheus`), or the DCGM ServiceMonitor is disabled / series carry no `tenant` label | See chapter 80 §80.4 |

---

## What's next

Chapter 19 picks up the parallel story for the SoT
integrations (Nautobot); chapter 39 in the customer book is
the user-facing version of this onboarding flow.
