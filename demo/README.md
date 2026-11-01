# Daalu demo lab

A small, **monitored** kind cluster for your Daalu to manage. Break things on
purpose and watch the agent find the problem and propose the fix. One command up,
one command down.

It creates a kind cluster `daalu-demo` on the machine you run it on, with:

- **kube-prometheus-stack**: Prometheus, Alertmanager, Grafana, kube-state-metrics
- **Loki + Promtail**: logs
- **dummy-app**: a trivial nginx app you can take down → fires **`DummyAppDown`**
- **metrics-app**: emits its own metrics; flip it into error mode → fires **`HighErrorRate`**

…then **connects the cluster to Daalu over the WireGuard edge tunnel** and
registers the **Prometheus**, **Loki** and **Kubernetes** integrations against it.

Install Daalu first: [docs/getting-started.md](../docs/getting-started.md)
(Docker Compose or Kubernetes). This cluster is separate from Daalu, even when
both run on the same machine.

## How it connects

```
  kind cluster "daalu-demo"                       Daalu
  ┌──────────────────────────┐                    ┌──────────────────────────┐
  │ kube-prometheus-stack     │                   │ UI · API · workers       │
  │ Loki / Promtail           │   WireGuard mesh  │ agent host · executor    │
  │ dummy-app / metrics-app   │══════════════════▶│ WireGuard hub :51820/udp │
  │ daalu-edge ─┬─ :6443 (kube API)               │                          │
  │             └─ :8888 (Prom/Loki proxy)        │                          │
  └──────────────────────────┘                    └──────────────────────────┘
        10.200.0.x ◀── tunnel IP allocated by Daalu on onboarding
```

The cluster dials **out** to the hub, so it needs no inbound ports. Daalu reaches
`kubectl` through the edge's API-server proxy (`<tunnelIp>:6443`) and Prometheus /
Loki through the edge's HTTP proxy (`<tunnelIp>:8888`).

## Prerequisites

- A running Daalu with `DAALU_HOST_IP` set (Compose) or installed by
  `make bootstrap-dev` (kind). See the getting-started guide.
- **docker, kind, kubectl, helm, curl, python3** (with PyYAML). Give Docker
  **~6 GB RAM** (the monitoring stack is the heavy part).
- inotify limits high enough for a second kind cluster:
  `sudo sysctl -w fs.inotify.max_user_instances=512 fs.inotify.max_user_watches=524288`
- Free host ports 3001 (Grafana), 9090 (Prometheus), 9093 (Alertmanager), 3100 (Loki).

## Bring it up

**From the UI** (shows the onboarding flow):

```bash
SKIP_ONBOARD=1 ./demo/up.sh          # cluster + monitoring + apps only
```

Then in Daalu: **Managed infra → Clusters & observability → Onboard cluster**, slug
`daalu-demo`. Run the `helm install daalu-edge …` command it prints with kubectl
pointed at `kind-daalu-demo`. When the cluster shows Connected, run the full
script once to register the integrations (it reuses the connected cluster):

```bash
DAALU_TOKEN=dpat_... ./demo/up.sh
```

**In one command:**

```bash
export DAALU_TOKEN=dpat_...          # Daalu → Settings → API tokens
./demo/up.sh                         # DAALU_API defaults to http://localhost:8000
```

Using the kind/Helm install of Daalu? Add `DAALU_API=http://localhost:8080`.

## Run the demo

```bash
./demo/break.sh            # bad-image (default) | crashloop | errors
./demo/status.sh           # app + alert state at any time
```

Open Daalu → **Alerts**. Within ~2–3 min `DummyAppDown` appears; open it and let
the agent investigate. It inspects the workload over the tunnel, finds the bad
rollout and proposes a **rollback** as a change proposal for you to approve. (Or
fix it yourself: `kubectl -n daalu-demo rollout undo deploy/dummy-app`.)

## Tear it down

```bash
DAALU_TOKEN=dpat_... ./demo/down.sh
```

Deletes the kind cluster **and** detaches it from Daalu (removes the three
integrations and the cluster row, freeing the tunnel IP).

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `DAALU_TOKEN` | _(required unless `SKIP_ONBOARD=1`)_ | `dpat_` tenant-admin token for the Daalu API |
| `DAALU_API` | `http://localhost:8000` | Daalu's API URL (`http://localhost:8080` for the kind install) |
| `CLUSTER_SLUG` | `daalu-demo` | the cluster's name in Daalu |
| `SKIP_ONBOARD` | `0` | `1` = build the cluster only; onboard it from the UI |
| `DEMO_BIND_ADDR` | `0.0.0.0` | host address the monitoring ports publish on |

## Troubleshooting

- **Pods crash with "too many open files"**: raise the inotify limits (above),
  then `./demo/down.sh` and `./demo/up.sh` again.
- **Tunnel stuck `awaiting_handshake`**: the edge can't reach UDP 51820 on
  `DAALU_HOST_IP`. Check the address and any host firewall, and the edge logs:
  `kubectl logs deploy/daalu-edge -c edge`.
- **Prometheus/Loki integration shows `error`**: the in-cluster Service name
  differs from the defaults (`prometheus-operated`, `loki`). List them with
  `kubectl -n monitoring get svc` and update the URL in the UI, keeping it bound
  to this cluster.
- **Onboarding says the cluster already exists**: run `./demo/down.sh` first to
  detach it, then `./demo/up.sh` again.

> A demo/learning lab, not production: it runs on a disposable cluster you can
> delete at any time.
