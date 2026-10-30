# 26. The join flow — from invite to handshake

End-to-end trace of what happens when a customer adds a
cluster. Useful when debugging why a tunnel never came up.

---

## The actors

- **U** — the operator (Daalu admin user).
- **API** — daalu-api pod.
- **HUB** — daalu-wireguard-hub pod.
- **HUB-API** — the sidecar API in the hub.
- **EDGE** — daalu-edge pod in the customer's cluster.
- **EDGE-BOOTSTRAP** — the bootstrap Job in the customer's
  cluster.

---

## Step 1: Invite generation

```
U → API: POST /api/v1/clusters/invite
        Body: { name: "acme-eu-prod" }
API → DB: INSERT into cluster_tunnels (name, status="pending", bootstrap_token_hash=hash(token), expires_at=now+1h)
API → U: Returns { token, helm_command }
```

The token is shown to the user once; only its hash is
stored. The helm_command is a pre-filled string with the
token interpolated:

```
helm install daalu-edge oci://... --set bootstrap.token=<token>
```

---

## Step 2: helm install (customer side)

```
U → cluster: helm install daalu-edge ...
cluster: kubectl apply <chart's manifests>
cluster: Pod daalu-edge-bootstrap-xxxxx Pending → Running
cluster: Pod daalu-edge-<deploy>-xxxxx Pending → Running (crashlooping; no Secret data yet)
```

The bootstrap Job and the Deployment are both deployed
together. The Deployment crashloops until the bootstrap
populates the Secret.

---

## Step 3: Bootstrap callback

```
EDGE-BOOTSTRAP: generate wg keypair → priv, pub
EDGE-BOOTSTRAP → API: POST /api/v1/clusters/<slug>/callback
                      Body: { token, public_key: pub }
API: verify token → look up cluster_tunnels row by hash(token)
API: check expiry, single-use
API → HUB-API: POST /peers { public_key: pub, allowed_ips: <assigned tunnel IP> }
HUB-API: wg set wg0 peer <pub> allowed-ips <ip> persistent-keepalive 25
HUB-API → API: { ok: true }
API → DB: UPDATE cluster_tunnels SET wg_pubkey=pub, tunnel_ip=<ip>, status="connecting", bootstrap_consumed=true
API → EDGE-BOOTSTRAP: 200 { hub_endpoint, hub_pubkey, tunnel_ip }
EDGE-BOOTSTRAP: write Secret { wg.conf: <config> }
EDGE-BOOTSTRAP: exit 0
```

---

## Step 4: Edge pod reads Secret

The Secret update triggers a restart of the daalu-edge
Deployment (because the Secret is mounted as a volume; we use
`kubectl rollout restart` triggered by the bootstrap as a
belt-and-braces).

```
EDGE Pod: start
EDGE Pod: wg-quick up wg0
EDGE Pod: WireGuard sends initial handshake to hub
```

---

## Step 5: First handshake

```
EDGE Pod → (encrypted UDP/51820) → HUB Pod
HUB Pod: handshake response
EDGE Pod: now reachable through tunnel at <tunnel IP>
```

The handshake takes ~1 second on a healthy network.

---

## Step 6: API verification

The next minute's cluster-tunnel-health agent run:

```
agent → HUB-API: GET /peers
HUB-API: returns peers with last-handshake times
agent → DB: UPDATE cluster_tunnels SET last_handshake_at=<time>, status="connected"
```

The cluster row in the UI flips from `connecting` to
`connected` within a minute.

---

## Step 7: First through-tunnel call

The operator clicks the cluster's detail page. The frontend
asks the API to fetch node info. The API:

```
API: kubectl get nodes (using the cluster's tunnel IP)
API: ... → http://<tunnel-ip>:<api-server-port> → edge pod → in-cluster kube-apiserver
```

The edge pod proxies the API request to the cluster's
in-cluster `kubernetes` Service via the SA token mounted in
the edge pod.

---

## Failure modes traced through

### A. Bootstrap call returns 401

```
EDGE-BOOTSTRAP → API: POST /callback { token, pubkey }
API: token not found OR expired OR already consumed
API → EDGE-BOOTSTRAP: 401 "invalid bootstrap token"
EDGE-BOOTSTRAP: log + exit 1
```

The Job goes into Failed state. Customer needs to regenerate
the invite.

### B. Hub sidecar fails to add peer

```
API → HUB-API: POST /peers
HUB-API: ... fails (wg command error)
HUB-API → API: 500
API: rollback transaction; cluster_tunnels stays in pending
API → EDGE-BOOTSTRAP: 500
```

Operator sees the failure in the cluster's detail page logs.
Probably a hub-side issue worth filing.

### C. Tunnel up, but kubectl-through-tunnel fails

```
HUB → EDGE: handshake OK; cluster row "connected"
API → http://tunnel-ip:<port>/api/...: connection refused / 403
```

Causes:

- A NetworkPolicy in the cluster blocks the edge pod from
  reaching the API server.
- The edge SA doesn't have RBAC for the requested action.
- The cluster's kube-apiserver isn't listening on the
  expected port.

The cluster detail page shows the exact error in the "API
reachability" section.

---

## What's next

Chapter 27 — cross-cluster access in general, including how
Prometheus / Loki / vLLM are reached through the tunnel.
