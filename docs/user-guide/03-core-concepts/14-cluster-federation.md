# 14. Cluster federation — managing your own infrastructure

Cloud accounts are easy: the cloud has a public API, Daalu calls
it. Kubernetes clusters you run on-prem or in a private subnet
are harder: there is no public API, and your security team will
not (correctly) let Daalu open inbound holes to reach them.

**Cluster federation** is how Daalu solves this. Each managed
cluster runs a small **Daalu Edge** agent that opens an
outbound-only WireGuard tunnel to the Daalu cloud. Through that
tunnel, the operator app can reach your cluster's API server,
your services, and your observability stack as if they were
local.

This chapter is the day-two view of federation — what's there,
what you see, what to do when it breaks. Chapter 34 covers the
initial setup; Chapter 22 in the engineer book covers the
internals.

---

## What it gives you

Once a cluster is federated, the following things start working:

- **The cluster shows up** as a row on *Managed infra →
  Clusters & observability* with status **connected** and a green dot.
- **The Assistant can query your cluster.** "List pods in
  default that are not Running." "Show me the last 20 lines of
  logs from deployment X." "What's the rollout status of Y?"
- **Drift between manifests and live cluster state** is
  detected. For now this is opt-in; see *Settings → Clusters →
  Reconciliation* to turn on.
- **Your Prometheus / Grafana / Loki running inside the cluster
  becomes a queryable target.** If you connect them as
  observability integrations (Chapter 31), Daalu reaches them
  through the same tunnel.
- **Local GPU inference** can run on the cluster if it has the
  hardware. See Chapter 15.

---

## What you see on Managed infra → Clusters & observability

Each federated cluster has a row with:

- **Name** — the friendly name you gave it during setup.
- **Tunnel IP** — the address Daalu uses to reach it through the
  WireGuard network (something like `10.200.0.42`).
- **Last handshake** — when the tunnel last completed a
  WireGuard handshake. Healthy is "<2 minutes ago."
- **Daalu Edge version** — the version of the agent running in
  the cluster.
- **API reachability** — green if the Daalu cloud can call
  `kubectl get nodes` through the tunnel.

Click the row for the detail view — node list, recent rollouts,
the Daalu Edge pod's own health, and a "Run a kubectl command"
panel that lets you fire one-shot commands through the tunnel
from the UI.

---

## Connection states and what they mean

A cluster row's status is one of:

- **Connected** (green). Tunnel up, recent handshake, API
  reachable. Normal.
- **Awaiting handshake** (amber). The tunnel was added but
  WireGuard hasn't completed a handshake. Usually this is a
  fresh deploy and just needs ~30 seconds. If it persists for
  more than a few minutes, something is wrong — see
  troubleshooting below.
- **Disconnected** (red). The tunnel has been down for more
  than 5 minutes. Daalu's view of the cluster is stale.
- **Error** (red). The handshake worked but the API isn't
  reachable. Usually a NetworkPolicy or missing service in the
  cluster.

Each state has a tooltip with the most recent diagnostic data.

---

## How to add a cluster

Quick summary; full details in Chapter 34.

1. *Managed infra → Clusters & observability → Onboard cluster.* Daalu generates a
   one-shot bootstrap invite (token + WireGuard public key,
   expiring in 1 hour).
2. In your cluster, install the **daalu-edge** Helm chart,
   passing the bootstrap token as a value.
3. The chart deploys a WireGuard pod and a small bootstrap pod.
   The bootstrap pod calls back to the Daalu cloud, exchanges
   the token for a long-lived WireGuard configuration, and
   exits.
4. The WireGuard pod brings up the tunnel. The cluster row
   turns **connected** within ~30 seconds.

There's no inbound port to open and no long-lived API key
embedded in your cluster. Revocation is "delete the Helm
release; the tunnel dies."

---

## When things go wrong

A few patterns we've seen many times.

### Awaiting handshake forever

If the row stays "awaiting handshake" for more than a few
minutes:

- **From the Daalu side:** the WireGuard peer might never have
  been configured with the cluster's public key. Refresh the
  page; if still awaiting, click the row and copy the diagnostic
  blob — support can read it.
- **From the cluster side:** the daalu-edge pod might not be
  running, or might be running but unable to send packets out
  (egress firewall). `kubectl -n daalu-edge get pods` and
  `kubectl -n daalu-edge logs deploy/daalu-edge` are the first
  two commands to run.

### Connected but API unreachable

The tunnel is up but kubectl-through-the-tunnel returns errors.
Common causes:

- A NetworkPolicy in the cluster blocks the edge pod from
  reaching the API server.
- RBAC on the edge pod's ServiceAccount doesn't grant the
  needed permissions.
- The cluster's API server isn't bound to the IP the edge pod
  is trying to reach.

The cluster detail page shows the exact error from the last
attempt. The support team uses these messages directly in
diagnostics.

### Disconnected after working

If a healthy tunnel goes red:

- **The edge pod was killed.** Check `kubectl -n daalu-edge get
  pods` — if it's restarting, examine logs and resource limits.
- **Egress from the cluster broke.** A firewall rule changed,
  a NAT gateway went down, your ISP had a moment. Verify
  external connectivity from the cluster itself.
- **The Daalu hub went down.** Very rare; if it ever happens
  it'll be on our status page.

A tunnel that's been down for more than 15 minutes also fires a
critical alert into your tenant — you'll find out before you
notice the symptoms.

---

## How many clusters?

There's no hard limit on cluster count per tenant. The Daalu hub
is engineered for hundreds of peers per tenant. Practical
considerations:

- **One IP per cluster.** Each federated cluster consumes one IP
  out of your tenant's tunnel subnet. The subnet is configured
  per tenant; default is `/24`, giving 254 clusters. Ask
  support to widen if you have more.
- **Latency.** Every kubectl-through-the-tunnel call is one
  round trip from Daalu's region to your cluster's network.
  Worldwide, this is 50–200 ms; usually invisible. For
  high-frequency operations it can compound.

---

## Security model

A few specifics for whoever reviews this for security:

- **The tunnel is outbound-only.** WireGuard is initiated from
  your cluster's daalu-edge pod to Daalu's hub. The hub never
  initiates inbound to your network.
- **Each tenant has its own WireGuard subnet.** Clusters from
  different tenants do not share an IP space.
- **The bootstrap token is single-use** and expires in 1 hour.
  Once consumed it's burned.
- **The long-lived WireGuard private key** lives in your
  cluster's daalu-edge Secret. It never leaves your cluster.
  The corresponding *public* key is what the Daalu hub
  configures as a peer.
- **API credentials inside your cluster** are whatever the
  daalu-edge ServiceAccount has via RBAC. You control the
  scope.
- **Daalu's cloud-side access** is logged. Every kubectl-
  through-tunnel call is a row in the audit log, with the
  caller (a user, the Assistant, or an agent) recorded.

Chapter 55 of the engineer book is the formal threat model. The
short version is "treat federation as if Daalu had a
read-restricted kubeconfig you handed it; the network layer adds
a second layer of defense."

---

## What's next

Chapter 15 covers local GPU and the LLM router — the third major
infrastructure-facing feature, and one that depends on cluster
federation to work.
