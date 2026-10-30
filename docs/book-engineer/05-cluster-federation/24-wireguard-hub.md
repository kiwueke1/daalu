# 24. The WireGuard hub

The cluster-federation feature is built on WireGuard. This
chapter covers how the hub on the Daalu side is deployed,
how peers are added, and the operational gotchas.

---

## What the hub does

`daalu-wireguard-hub` is a Deployment in the operator-app
namespace. It runs:

- WireGuard on UDP/51820 (exposed via a NodePort/LoadBalancer
  Service).
- A small sidecar API that adds and removes peers
  dynamically (no need to restart WireGuard).

Tunnels terminate at the hub. From the hub's POV, each
federated cluster is one peer on the `wg0` interface.

---

## Subnets

Each tenant gets its own `/24` from a private pool:

- Default base: `10.200.0.0/16`.
- Allocator: per-tenant `/24` (e.g., `10.200.0.0/24`,
  `10.200.1.0/24`, ...).
- Per-tunnel IP: assigned within the tenant's `/24`,
  starting at `.2` (`.1` reserved for the hub side).

Configurable in `values.yaml`:

```yaml
wireguardHub:
  basePrefix: 10.200.0.0/16
  perTenantPrefix: /24
```

For very large tenants (>250 clusters) we widen on request.

---

## Peer registration flow

When a customer installs daalu-edge, the bootstrap exchange:

1. Edge pod generates a fresh WireGuard private+public key
   pair on first start.
2. Edge POSTs the bootstrap token + public key to
   `https://hub.example.com/api/v1/clusters/<slug>/callback`.
3. The callback handler:
   - Verifies the bootstrap token (single-use, expires 1
     hour).
   - Allocates an IP from the tenant's `/24`.
   - Calls the hub sidecar's `add-peer` API with the
     edge's public key and the assigned IP.
   - Updates the `cluster_tunnels` row with the assigned IP.
   - Returns the WireGuard server's endpoint, public key,
     and the assigned IP.
4. Edge writes the WireGuard config and brings up `wg0`.

The bootstrap is burned after one use.

---

## How peers actually get onto the interface: the Secret + syncconf loop

There is no imperative "add-peer API". The coordination is declarative
(`core/wireguard.py`, module docstring at the top of the file):

- The `cluster_tunnels` table is the source of truth (keypairs, allocated
  tunnel IPs, the customer-reported pubkey/endpoint).
- `sync_hub_secret(db)` renders the full `wg0.conf` from every row in
  `awaiting_handshake`/`connected`/`degraded` (`render_hub_config`,
  `core/wireguard.py:177`) and **patches the `daalu-wireguard-peers`
  Secret** in-cluster. Both the `POST /clusters` router and the
  tunnel-health beat task call it after mutating rows, so the Secret is
  always derivable from the DB.
- The hub pod mounts that Secret and its entrypoint
  (`deploy/wg-hub/entrypoint.sh`) reconciles the live interface with it.

### The reload bug and the fix (PR #106)

The reconcile used to be an `inotifywait` on the *file path* `wg0.conf`.
kubelet updates a secret-mounted volume by writing a fresh timestamped
directory and **atomically swapping the `..data` symlink** — it never
modifies `wg0.conf` in place. A file-path watch resolves the symlink to a
concrete inode at watch-setup time, so the *first* `..data` swap deleted the
watched inode, dropped the watch, and every later peer add was **silently
missed**. New clusters then sat on "awaiting handshake" until someone
restarted the hub or ran `wg syncconf` by hand in the pod.

`deploy/wg-hub/entrypoint.sh` now reconciles belt-and-suspenders:

1. **watch the mount directory** (a stable inode) for `create`/`moved_to`
   events on `..data`/`wg0.conf` — near-instant reaction to a new peer; and
2. run an **unconditional periodic `wg syncconf`** every 15 s
   (`WG_RECONCILE_INTERVAL`) as a guaranteed-convergence backstop.

`wg syncconf` is idempotent and only diffs peers — it never tears down a live
handshake — so re-running it every tick is cheap and safe. The one thing it
*cannot* do is swap the interface's own private key (`wg-quick strip` /
`[Interface]` changes), so a hub key rotation still needs a pod restart. If
either reconcile loop dies the entrypoint exits so the pod restarts, rather
than silently ceasing to reconcile.

Also from #106: the hub Deployment runs `imagePullPolicy: Always`
(`deploy/k8s/wg-hub/deployment.yaml:50`) — CI rebuilds `:latest` in place, so
`IfNotPresent` kept a stale image on redeploy.

**Operational consequence:** after hand-editing the `daalu-wireguard-peers`
Secret (e.g. during re-onboarding — see chapter 25), the hub converges on its own within
kubelet's secret-sync period + one tick. **Do not** roll the Deployment out of
habit; that was only ever necessary because of the broken watch.

---

## NetworkPolicy

The hub's WireGuard pod needs ingress from anywhere on
UDP/51820 (customers connect from arbitrary egress IPs).
The hub itself only egresses inside the cluster's own
namespaces.

NetworkPolicy on the operator side allows:

- Ingress to `wg-hub` Service on UDP/51820 from any.
- Egress from `wg-hub` to `daalu-api` Service on TCP/80
  (the callback handler).
- Egress from `wg-hub` to any pod in tenant-tunnel-IP space
  on any port (so the operator app can reach customer
  services through the tunnel).

The cluster's CNI must support UDP — k3s + flannel does. AKS
in basic mode does too. OpenShift in restricted SCC may need
specific allowances.

---

## Routing

Inside the hub pod, the routing table sends
`10.200.0.0/16` traffic out the `wg0` interface. WireGuard's
allowed-ips per peer determines which packet goes where.

From the operator app's pod's POV:

```
curl http://10.200.0.2:8000/v1/models
```

→ kernel routes via `wg0` → WireGuard encrypts and forwards
to the right peer.

The pod doesn't need any special configuration; it just
treats the tunnel IP as a normal address.

---

## Operational gotchas

A few things we've learned:

- **NAT keepalives.** Some customer NAT devices have short
  UDP timeouts. We set `PersistentKeepalive = 25` on every
  peer config to keep tunnels alive.
- **Pod scheduling.** The hub pod needs to be on a node
  with stable external IP exposure. We pin to a single node
  via `nodeSelector`.
- **MTU.** WireGuard adds ~60 bytes overhead. We set `MTU =
  1380` on both ends to avoid fragmentation.

---

## Monitoring

The hub exposes metrics on `:9100/metrics` scraped by a
ServiceMonitor:

- `wireguard_peer_last_handshake_seconds` — per-peer
  freshness.
- `wireguard_peer_transfer_rx_bytes` / `_tx_bytes` —
  per-peer traffic.
- `wireguard_peers_total` — peer count.

The cluster-tunnel-health agent polls these every minute and
fires alerts on stale peers.

---

## What's next

Chapter 25 — the customer-side daalu-edge chart.
