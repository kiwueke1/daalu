# 25. Deploying daalu-edge to a workload cluster

The customer-side federation chart. Lives at
`deploy/helm/daalu-edge/`.

This chapter explains what the chart deploys, how it
interacts with the hub, and what configuration knobs exist.

---

## What the chart deploys

In a fresh install, the chart creates the `daalu-edge`
namespace and:

- **daalu-edge Deployment** — the WireGuard pod.
- **daalu-edge-bootstrap Job** — one-shot bootstrap that
  exchanges the token for a long-lived config.
- **Secret** — holds WireGuard private key (after bootstrap).
- **ConfigMap** — runtime values (hub URL, log level).
- **ServiceAccount** — what the pod runs as.
- **RBAC** — perms the pod needs to talk to the cluster's
  API server on Daalu's behalf.
- **Service** — internal endpoint for the bootstrap to call
  back to.

---

## Bootstrap Job

The Job runs once on chart install. Its purpose:

1. Generate a WireGuard private+public key pair.
2. POST to the hub:
   ```
   POST https://hub.example.com/api/v1/clusters/<slug>/callback
   Body: {
     "token": "<bootstrap token>",
     "public_key": "<edge wg pubkey>"
   }
   ```
3. Receive:
   ```json
   {
     "hub_endpoint": "wg.example.com:51820",
     "hub_public_key": "...",
     "tunnel_ip": "10.200.0.42/32"
   }
   ```
4. Write the WireGuard config into the Secret.
5. Exit.

The Deployment's pod template reads the Secret on startup
and brings up `wg0`. The Deployment was already running
(maybe crashing because the Secret didn't have data yet); on
the Secret update, Kubernetes restarts the pod and it comes
up clean.

---

## Pod spec

The edge pod runs:

- **wg-quick container** — sets up the WireGuard interface.
  Needs `CAP_NET_ADMIN` and `CAP_SYS_MODULE`.
- **wg-resync sidecar** (optional, advanced) — keeps the
  interface healthy by re-applying config on changes.

The pod uses `hostNetwork: false` — WireGuard works fine
inside a normal Pod network namespace.

---

## The forward proxy (`deploy/edge/proxy.py`)

The edge also runs the **forward proxy** on the tunnel IP (`:8888`) — the
hub's one door into the customer cluster's private address space. It serves
two request shapes:

- **Plain HTTP forward** — the hub dials `http://<tunnel_ip>:8888` as an
  httpx proxy; the edge resolves the in-cluster `*.svc` target and honours
  `X-Daalu-Forward-Host` (re-applied as the upstream `Host`, then stripped —
  the cmtools tool proxy and the SoT adapter both rely on this; see chapters
  15 and 68).
- **Raw TCP splice via HTTP `CONNECT`** — how non-HTTP protocols (SSH for
  the LLDP probe, the device terminal, `linux_ssh` executor pushes) reach
  RFC1918 device addresses that only route inside the cluster
  (`core/cluster_proxy.py:open_connect_socket`, chapter 79 §79.3).

Security model: the proxy listens **only on the `wg0` tunnel IP** — the
per-peer WireGuard cryptography is the auth boundary — and an
`ALLOWED_HOSTS` regex (default `*.svc(.cluster.local)` + RFC1918 literals)
blocks public dial-outs, so a compromised hub cannot turn the edge into an
exfiltration relay.

> **The CONNECT path was dead code until PR #129.** aiohttp never routes a
> CONNECT request's authority-form target (`host:port`) to a wildcard route,
> so the handler had literally never run. It is now intercepted in a
> middleware (`proxy.py:213-225`), reads the authority from
> `request.raw_path`, and detaches aiohttp by swapping the transport's
> protocol for a plain `StreamReaderProtocol` before splicing
> (`proxy.py:189-194`). Full detail in chapter 79 §79.4. An edge image older
> than this fix silently breaks every SSH-over-tunnel feature — check the
> image tag before debugging anything else.

---

## Cluster-credential self-registration (PR #107)

The chart also creates a **ServiceAccount + long-lived token Secret**, and
the edge entrypoint reports that token in the bootstrap callback it already
makes. The hub assembles a kubeconfig from it (apiserver via the tunnel) and
**upserts it as the cluster's `kubernetes` Integration** — replacing the old
manual step of hand-minting a token, assembling a kubeconfig, and pasting it
into Integrations → Kubernetes (which was easy to forget and silently
invalidated by a cluster rebuild, surfacing much later as a bare 401 from the
customer's apiserver). A rebuilt cluster replaces its own stale credential
instead of stranding one. This is what makes GPU onboarding zero-touch —
chapter 80.

---

## Values

`deploy/helm/daalu-edge/values.yaml`:

```yaml
hub:
  url: https://hub.example.com
  endpoint: wg.example.com:51820

bootstrap:
  token: ""                  # set at install time
  timeout: 120

wg:
  mtu: 1380
  keepalive: 25
  logLevel: info

image:
  repository: ghcr.io/kiwueke1/daalu-edge
  tag: latest                # or pinned SHA
  pullPolicy: IfNotPresent

resources:
  requests:
    cpu: 50m
    memory: 64Mi
  limits:
    cpu: 200m
    memory: 256Mi

rbac:
  enabled: true
  readPodLogs: true
  readEvents: true
  readDeployments: true
```

RBAC granularity is the value most likely to be customized.
Customers who want a wider Daalu reach can enable additional
permissions; customers who want narrower can disable.

---

## Updates

`helm upgrade daalu-edge oci://ghcr.io/kiwueke1/daalu-edge`
pulls the latest chart and re-deploys. The pod restarts but
the WireGuard config persists in the Secret so the tunnel
reconnects in seconds.

Bumping the image:

```bash
helm upgrade daalu-edge oci://ghcr.io/kiwueke1/daalu-edge \
  --set image.tag=<new-sha>
```

---

## Removing

`helm uninstall daalu-edge`:

- Deletes the Deployment (tunnel dies).
- Deletes the Secret.
- Deletes the Job.
- Daalu side: cluster row goes red within 30s, fires alert
  at 15 min.

On the Daalu side, deleting the cluster row from the UI
removes the peer from the hub.

---

## Multi-replica?

Currently no — the edge pod is single-replica. We considered
HA edge but the cost/benefit isn't there for most customers:

- WireGuard handles brief outages gracefully (no in-flight
  sessions to drop).
- A pod restart reconnects in seconds.
- An HA setup would need keepalived-style failover, adding
  complexity for marginal benefit.

If a customer needs absolute uptime, a single pod with
guaranteed PriorityClass is the right pattern.

---

## What's next

Chapter 26 — the join flow from invite to handshake in
sequence diagram form.
