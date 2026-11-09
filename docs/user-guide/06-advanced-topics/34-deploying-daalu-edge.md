# 34. Deploying daalu-edge to your own cluster

This is the deepest chapter in this handbook. You'll run `helm
install` once. Beyond that, the customer-facing experience is
"the cluster row turns green."

If you don't operate a Kubernetes cluster yourself, you can skip
this entire chapter — federation is opt-in.

---

## Prerequisites

- A Kubernetes cluster you operate (k3s, EKS, GKE, AKS,
  whatever).
- `kubectl` configured to talk to it, with permissions to
  install Helm charts into a new namespace.
- `helm` v3 installed locally.
- Outbound HTTPS from the cluster to `hub.example.com:443` and
  outbound WireGuard (UDP/51820) to the same.
- A Daalu admin account in the tenant where you want this
  cluster to belong.

The outbound-only nature of the connection means your firewall
doesn't need any inbound holes.

---

## Step 1: Generate a bootstrap invite

In the operator app:

1. *Managed infra → Clusters & observability → Onboard cluster.*
2. Pick a friendly name (e.g., `acme-eu-prod`).
3. Choose the tenant's WireGuard subnet allocation. Default
   is fine.
4. Click **Generate invite.** Daalu produces:
   - A **bootstrap token** (single-use, expires in 1 hour).
   - A copy-paste **helm install** command pre-filled with
     the token.

Copy the command. It looks like:

```bash
helm install daalu-edge oci://ghcr.io/daalu/charts/daalu-edge \
  --namespace daalu-edge --create-namespace \
  --set hub.url=https://hub.example.com \
  --set bootstrap.token=<long-random-token>
```

---

## Step 2: Install the chart

Run the command. It deploys:

- **daalu-wireguard-edge** — the WireGuard pod.
- **daalu-edge-bootstrap** — a Job that exchanges the
  bootstrap token for a long-lived WireGuard configuration.
- Supporting Secret, ConfigMap, RBAC.

The bootstrap Job:

1. POSTs the token to `hub.example.com` with the cluster's
   freshly-generated WireGuard public key.
2. Receives back the WireGuard server endpoint and the IP
   address assigned to this cluster in the tenant's subnet.
3. Writes a Secret with the WireGuard config.
4. Exits successfully.

The WireGuard pod consumes the Secret and brings up the
tunnel. Within ~30 seconds you should see a successful
handshake.

The edge also **self-registers the cluster credential** with
Daalu in the same bootstrap: the chart creates a scoped
ServiceAccount, and the edge reports its token so the hub can
assemble the cluster's Kubernetes access itself. You never
hand-mint a token or paste a kubeconfig — and if you ever
rebuild the cluster and re-onboard it, the fresh edge replaces
its own stale credential automatically instead of stranding a
dead one.

---

## Step 3: Verify in the operator app

Back in *Managed infra → Clusters & observability*, the new row should be:

- **Status: connected** (green).
- **Tunnel IP**: a `10.200.0.x` address.
- **Last handshake:** <2 minutes ago.

Click the row for the detail page. Verify:

- The node list shows your cluster's nodes.
- The "kubectl run" panel can execute `kubectl get nodes`
  through the tunnel.

If any of this is off, see the troubleshooting section.

---

## What got installed in your cluster

After Step 2, in the `daalu-edge` namespace you'll have:

- **A Deployment** running the WireGuard pod (1 replica).
- **A Secret** holding the WireGuard private key.
- **A ConfigMap** with the public WireGuard config.
- **A ServiceAccount** the edge pod runs as.
- **RBAC** granting the SA permissions to talk to the API
  server on Daalu's behalf.

The RBAC is scoped by default to:

- Read pods, services, deployments, statefulsets, daemonsets
  cluster-wide.
- Read events.
- Read logs (`pods/log`).

To widen this (so the Assistant can read more, or so write
proposals can be applied), edit *Settings → Clusters → \[cluster\]
→ RBAC.* Daalu generates the patch for you to apply.

---

## What didn't get installed

A few things that are explicitly *not* present:

- **No daemonset on every node.** The edge is one pod on one
  node. Crashlooping doesn't affect data-plane workloads.
- **No mTLS reverse-proxy or sidecar in your pods.** Nothing
  in your existing workloads changes.
- **No NodeLocal DNS hijacking, no CNI plugin, no privileged
  container.** The WireGuard pod runs with `NET_ADMIN` for
  its WireGuard interface only. Nothing else is privileged.

---

## Operational properties

- **Restart behaviour.** If the edge pod dies, Kubernetes
  restarts it. The tunnel reconnects in seconds. The Daalu
  side notes the dropped handshake; if it's down for >15
  minutes, a critical alert fires.
- **Cluster reboots.** Reboots are transparent. Once the edge
  pod schedules and starts, the tunnel comes back.
- **Updates.** The chart can be upgraded with `helm upgrade`.
  We publish releases as needed; check the integration detail
  page for "Available update."

---

## Removing daalu-edge

`helm uninstall -n daalu-edge daalu-edge` and the tunnel dies
immediately. In the operator app, the cluster row will go to
**disconnected** within seconds. Delete the cluster row from
the UI to clean up the Daalu side as well.

---

## Troubleshooting

### "Awaiting handshake" persists for >5 minutes

Most common causes:

- **Egress firewall.** The edge pod can't reach
  `hub.example.com` on UDP/51820. Verify your egress policy
  permits this.
- **The bootstrap Job failed.** Check `kubectl -n daalu-edge
  get jobs` — if `daalu-edge-bootstrap` shows a failure,
  inspect its logs: `kubectl -n daalu-edge logs job/daalu-
  edge-bootstrap`.
- **Bootstrap token expired.** The token is valid for 1
  hour. If you generated the invite ages ago, regenerate.

### Connection drops periodically

- **Aggressive NAT timeouts.** The edge pod sends a keepalive
  every 25 s by default; some NAT devices have shorter
  windows. Lower the interval via the chart's
  `wg.keepalive` value.
- **Pod evictions.** If your cluster is rescheduling the edge
  pod often, increase its priority or pin it to a stable
  node.

### kubectl-through-tunnel fails

- **RBAC.** The edge SA doesn't have permission to do what
  Daalu asked. Inspect the error message; widen RBAC or
  narrow the Daalu request.
- **NetworkPolicy.** Your cluster has a NetworkPolicy that
  forbids the edge pod from reaching the API server. Add an
  exception.

---

## What's next

Chapter 35 covers the local-GPU side — connecting a GPU in one
of these clusters once federation is up. It's a single form:
the cluster-onboarding you just did is the only command-line
step in the whole path.
