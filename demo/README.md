# Daalu demo lab (hosted)

Stand up a small, **monitored** test cluster and connect it to your **hosted
Daalu at `daalu.example.com`** — then break things on purpose and watch the agent
discover the issue and propose the fix. One command up, one command down.

It deploys, in a throwaway [kind](https://kind.sigs.k8s.io) cluster on the
machine you run it on (e.g. a lab box):

- **kube-prometheus-stack** — Prometheus, Alertmanager, Grafana, kube-state-metrics
- **Loki + Promtail** — log aggregation
- **dummy-app** — a trivial nginx app you can take down → fires **`DummyAppDown`**
- **metrics-app** — emits its own metrics; flip it into error mode → fires **`HighErrorRate`**

…then **onboards the cluster to the hub over the WireGuard edge tunnel** and
registers the **Prometheus**, **Loki**, and **Kubernetes** integrations against
it — so the agent can see + fix issues exactly as it does for your real cluster.

> This is the **hosted** variant. The hosted Daalu doesn't share a network with
> your cluster, so — unlike a single-box install — it reaches this cluster through
> the `daalu-edge` tunnel: `kubectl` via the edge's apiserver-proxy
> (`<tunnelIp>:6443`), and Prometheus/Loki via the edge's in-cluster HTTP proxy
> (`<tunnelIp>:8888`, bound through each integration's `cluster_tunnel_id`).

## How it connects (topology)

```
  kind cluster (lab machine)                     your Daalu hub          
  ┌─────────────────────────┐                    ┌──────────────────────┐
  │ kube-prometheus-stack    │                   │ daalu-api + UI       │
  │ Loki / Promtail          │   WireGuard mesh  │ wireguard-hub        │
  │ dummy-app / metrics-app  │◀═════════════════▶│  (wg.example.com:51820) │
  │ daalu-edge  ──┬─ :6443 (kubectl)             │                      │
  │               └─ :8888 (Prom/Loki proxy)     │                      │
  └─────────────────────────┘                    └──────────────────────┘
        10.200.0.x  ◀── tunnel IP allocated by the hub on onboard ──┘
```

The cluster dials **out** to the hub (NAT-friendly — no inbound firewall rules
on gpu-node-1). This is the same mechanism your production gpu-node-1/gpu-node-2 cluster uses.

## Prerequisites

- **Run it on gpu-node-1 (or gpu-node-2)** — any Docker host that can reach
  `wg.example.com:51820/UDP` outbound. (Tunnel traffic is outbound-initiated.)
- **docker, kind, kubectl, helm, curl, python3** on that host. Give Docker
  **~6 GB RAM** (the monitoring stack is the heavy part).
- The kind cluster must be able to **pull `ghcr.io/kiwueke1/daalu-edge`**. If
  that image is private, create a pull secret first (see *Troubleshooting*).
- The hub side must already be set up (it is, since gpu-node-1/gpu-node-2 are connected):
  `WIREGUARD_PUBLIC_ENDPOINT` + `OPERATOR_API_PUBLIC_URL` on the ops cluster —
  see [`docs/cluster-tunnel-feature.md`](../docs/cluster-tunnel-feature.md) §0.
- A **`dpat_` personal access token** for a tenant-admin user (next section).

### Get an API token

In the ops UI (`https://daalu.example.com`) → **Settings → API tokens** → create a
token (it's shown once). Or via the API:

```bash
JWT=$(curl -fsS -X POST https://daalu.example.com/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"you@daalu.io","password":"…"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -fsS -X POST https://daalu.example.com/api/v1/auth/tokens \
  -H "Authorization: Bearer $JWT" -H 'content-type: application/json' \
  -d '{"name":"demo-lab","expires_in_days":30}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])'
```

## Deploy it (one command)

On gpu-node-1, from the repo root:

```bash
export DAALU_TOKEN=dpat_xxx          # the token from above
./demo/up.sh                         # DAALU_API defaults to https://daalu.example.com
```

`up.sh` creates the kind cluster + monitoring + apps, onboards the cluster to
the hub (gets a tunnel IP + one-shot install token), runs the `daalu-edge` helm
chart, waits for the tunnel to connect, and registers the three integrations.
It prints the Grafana/Prometheus/Alertmanager URLs (browsable on gpu-node-1).

## Run the demo

```bash
./demo/break.sh            # break the app (bad-image | crashloop | errors)
./demo/status.sh           # app + alert state at any time
```

Then open **`https://daalu.example.com` → Alerts**. Within ~2–3 min `DummyAppDown`
appears; open it and let the agent investigate — it inspects the workload over
the tunnel, finds the bad rollout, and proposes a **rollback** as a Change
Proposal for you to approve. (Or fix it yourself:
`kubectl -n daalu-demo rollout undo deploy/dummy-app`.)

## Tear it down (one command)

```bash
DAALU_TOKEN=dpat_xxx ./demo/down.sh
```

Deletes the kind cluster **and** detaches it from the hub (removes the three
integrations + the cluster/tunnel row, freeing the tunnel IP).

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `DAALU_TOKEN` | _(required)_ | `dpat_` tenant-admin PAT for the hub API |
| `DAALU_API` | `https://daalu.example.com` | hub API base URL |
| `CLUSTER_SLUG` | `daalu-demo` | the cluster's tunnel slug on the hub |
| `DEMO_BIND_ADDR` | `0.0.0.0` | host address the monitoring NodePorts publish on (browser only) |

## Troubleshooting

- **`daalu-edge` pod ImagePullBackOff** — the edge image is private. Create a
  ghcr pull secret in the edge's namespace and reference it, e.g.:
  ```bash
  kubectl create secret docker-registry ghcr --docker-server=ghcr.io \
    --docker-username=<gh-user> --docker-password=<gh-PAT-with-read:packages>
  helm upgrade daalu-edge oci://ghcr.io/kiwueke1/charts/daalu-edge --reuse-values \
    --set 'imagePullSecrets[0].name=ghcr'
  ```
- **Tunnel stuck `awaiting_handshake`** — the edge can't reach
  `wg.example.com:51820/UDP`. Check outbound UDP from gpu-node-1 and the edge pod logs:
  `kubectl logs deploy/daalu-edge -c edge`.
- **Prometheus/Loki integration shows `error`** — the in-cluster Service name
  differs from the defaults (`alertmanager-operated`, `loki`). List them with
  `kubectl -n monitoring get svc` and update the integration URL in the UI
  (Managed infra → Observability), keeping it bound to this cluster's tunnel.
- **Onboard says the cluster already exists** — run `./demo/down.sh` first to
  detach it (re-mints a fresh one-shot token), then `./demo/up.sh` again.

> Demo/learning lab, not production: it runs on a disposable cluster you can
> delete at any time.
