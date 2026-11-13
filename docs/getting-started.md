# Getting started

This guide takes you from a fresh `git clone` to a running Daalu that is managing
a separate Kubernetes cluster, with real alerts for the AI agent to investigate.
Everything runs on one Linux machine.

There are two ways to install Daalu. Pick one:

| | **Method 1: Docker Compose** | **Method 2: Kubernetes (Helm)** |
|---|---|---|
| Best for | Trying Daalu, demos, a laptop | A production-like install, or a real cluster |
| You need | Docker | Docker, kind, kubectl, helm (or your own cluster) |
| Daalu runs as | Containers on the host | Pods in a Kubernetes cluster, installed by the Helm chart |
| Start it with | `docker compose up -d --build` | `make bootstrap-dev` (kind) or `helm install` (your cluster) |
| UI at | http://localhost:3000 | http://localhost:8080 (kind) or your hostname |
| Time | ~10 min (mostly image builds) | ~15 min |

Both methods run the same images and the same services: the API, a Celery worker
and scheduler, the agent host, the executor, the web UI, Postgres, Redis, and the
**WireGuard hub** that clusters connect to. Whichever you choose, you then
[add a cluster for Daalu to manage](#3-add-a-cluster-for-daalu-to-manage). That
cluster is separate from Daalu: it reaches Daalu over an outbound WireGuard tunnel,
the same way a cluster in another data center or behind NAT would.

```
  this machine
  ┌──────────────────────────────────────────────────────────────────────┐
  │  Daalu (Compose or kind)              managed cluster (kind)         │
  │  ┌───────────────────────┐            ┌───────────────────────────┐  │
  │  │ UI · API · workers    │  WireGuard │ daalu-edge                │  │
  │  │ agent host · executor │◀═══════════│  ├─ :6443  → kube API     │  │
  │  │ WireGuard hub :51820  │  10.200/16 │  └─ :8888  → Prom / Loki  │  │
  │  │ Postgres · Redis      │            │ Prometheus · Alertmanager │  │
  │  └───────────────────────┘            │ Grafana · Loki            │  │
  │                                       │ dummy-app · metrics-app   │  │
  │                                       └───────────────────────────┘  │
  └──────────────────────────────────────────────────────────────────────┘
```

## 0. Prerequisites

- **Linux** with Docker Engine 24+ and the Compose plugin (`docker compose version`).
  The WireGuard hub needs the kernel's `wireguard` module, which ships with any
  kernel from 5.6 on.
- **kind, kubectl, helm** for the managed cluster (and for Method 2).
  `python3` with PyYAML (`python3 -c 'import yaml'`) for the demo script.
- **Memory:** 8 GB free for Daalu plus the monitored demo cluster.
- **inotify limits** high enough for kind (the default 128 instances makes pods
  in a second kind cluster fail with "too many open files"):

  ```bash
  sudo sysctl -w fs.inotify.max_user_instances=512 fs.inotify.max_user_watches=524288
  # persist: put both settings in /etc/sysctl.d/99-kind.conf
  ```
- **Free ports:** 3000, 8000 (Method 1) or 8080 (Method 2), and UDP 51820 for
  the hub. The demo cluster also publishes 3001, 9090, 9093 and 3100.
- **A model (optional, recommended).** The AI parts need an LLM. Any of these works:
  - your own OpenAI-compatible server (vLLM, llama.cpp, Ollama):
    `LLM_LOCAL_BASE_URL=http://<host>:<port>/v1` and `LLM_LOCAL_MODEL_CLASSIFIER=<model name>`
  - a GPU in a cluster you connect: Daalu deploys the model onto it for you
    ([section 4](#4-run-daalus-ai-on-your-own-gpu-optional)), with nothing to set here
  - a hosted OpenAI-compatible API: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`
  - Anthropic: `ANTHROPIC_API_KEY`

  With no model configured, Daalu still runs, and alert triage and briefings use
  deterministic fallbacks instead of the agent.

Clone the repo and create your `.env`:

```bash
git clone https://github.com/kiwueke1/daalu.git
cd daalu
cp .env.example .env
```

Find the IP address other machines (and the kind cluster) reach this one on.
You'll need it in both methods:

```bash
hostname -I | awk '{print $1}'      # e.g. 192.168.0.77
```

On a machine with several addresses, the first one isn't always right: use the one
the managed cluster's nodes reach this machine on (`ping` it from a node).

---

## 1. Method 1: Docker Compose (quick start)

### 1.1 Configure

Edit `.env` and set the first block:

```bash
ADMIN_EMAIL=you@example.com
ADMIN_PASSWORD=<choose a password>
DAALU_HOST_IP=192.168.0.77          # from `hostname -I`
SECRET_KEY=<output of: openssl rand -hex 32>
SERVICE_TOKEN_SECRET_KEY=<another: openssl rand -hex 32>
```

`SERVICE_TOKEN_SECRET_KEY` signs calls between Daalu's own services (for example
when you add a GPU); `docker compose up` refuses to start without it.

`ADMIN_EMAIL` needs an ordinary domain: login rejects reserved ones such as `.local`,
`.test` and `.localhost`. No mail is sent to it.

Add your model settings (see [Prerequisites](#0-prerequisites)) in the LLM
sections of the same file. Leave everything else at its default: Compose points
the containers at its own Postgres and Redis, whatever the `localhost` URLs in
`.env` say.

### 1.2 Start

```bash
docker compose up -d --build
```

The first run builds three images (backend, web UI, WireGuard hub), which takes
5–10 minutes. Then Compose starts Postgres and Redis, runs a one-shot `migrate`
container (creates the schema, the default tenant and your admin login), and
starts the rest. Check that everything is up:

```bash
docker compose ps
```

Every service should be `running` and `api` should say `healthy`; `migrate` shows
`exited (0)`, which is expected.

### 1.3 Sign in

Open **http://localhost:3000** (or `http://<DAALU_HOST_IP>:3000` from another
machine) and sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`.

The API is on **http://localhost:8000** (interactive docs at `/docs`).

### What's running

| Service | What it does |
|---|---|
| `frontend` | Next.js web UI on :3000; proxies `/api` to the API |
| `api` | FastAPI on :8000: REST, server-sent events, auth |
| `worker` | Celery worker: integrations polling, alert ingestion, tunnel health |
| `beat` | Celery scheduler (briefings, polling cadence) |
| `agents` | Agent host: listens to the event stream and runs alert triage |
| `executor` | Runs approved change proposals |
| `gpu-controller` | Deploys and runs the model on a connected cluster's GPU (AI Factory → Add GPU) |
| `wg-hub` | WireGuard hub on UDP :51820 that managed clusters dial |
| `net` | Holds the shared network namespace (like a pod's pause container); the API's port 8000 is published here |
| `postgres`, `redis` | State and queues (data persists in the `pgdata` volume) |
| `migrate` | One-shot: schema, default tenant, admin login. Runs on every `up` |

The WireGuard hub, API, worker, agent host, executor and GPU controller share one network
namespace, owned by the small `net` container, so they reach managed clusters
on their tunnel addresses (`10.200.0.0/16`) directly. That's also why the
API's port is published on the `net` service.

### Day-2 commands

```bash
docker compose logs -f api worker agents      # follow logs
docker compose up -d --build                  # rebuild + restart after a git pull
docker compose down                           # stop (keeps the database)
docker compose down -v                        # stop and delete all data
```

Restarting any one service, including `wg-hub`, leaves the others' networking
intact: the shared namespace belongs to `net`, not to the hub.

Next: [add a cluster for Daalu to manage](#3-add-a-cluster-for-daalu-to-manage).

---

## 2. Method 2: Kubernetes (Helm)

The supported production install is the Helm chart in
[`deploy/helm/daalu-automation`](../deploy/helm/daalu-automation). It runs every
service as a Kubernetes workload, with the WireGuard hub as a host-network pod on
a labelled node. You can try it on a local kind cluster (2a) or install it on a
real cluster (2b).

### 2a. On a local kind cluster

```bash
# optional: choose the login (otherwise a random password is printed)
export ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='<choose a password>'
make bootstrap-dev
```

[`dev/bootstrap.sh`](../dev/bootstrap.sh) does six things and prints each step:

1. creates the kind cluster `daalu-dev` from [`dev/kind-cluster.yaml`](../dev/kind-cluster.yaml)
   (one node, labelled `daalu.io/wireguard-hub=true`; host ports 8080, 8443 and UDP 51820)
2. installs ingress-nginx
3. builds the backend, web UI and WireGuard-hub images and loads them into kind
   (no registry needed)
4. creates the `daalu-automation-secrets` Secret from your `.env`, pointing the
   database and Redis URLs at the in-cluster services, and sets the hub endpoint
   to this machine's IP (`DAALU_HOST_IP`, default: first address of `hostname -I`)
5. runs `helm upgrade --install` with [`dev/values-kind.yaml`](../dev/values-kind.yaml)
   (plain HTTP, one replica each, a freshly generated hub key)
6. seeds the default tenant and creates your admin login

Then open **http://localhost:8080** and sign in. Check the pods:

```bash
kubectl -n daalu-automation get pods
# daalu-api, daalu-worker, daalu-beat, daalu-agents, daalu-executor,
# daalu-frontend, daalu-gpu-controller, daalu-postgres, daalu-redis,
# daalu-wireguard-hub
```

Port 8080 taken? `DAALU_HTTP_PORT=18080 make bootstrap-dev`. Re-running
`make bootstrap-dev` rebuilds the images and upgrades the release.
`WITH_MONITORING=1 make bootstrap-dev` also installs kube-prometheus-stack into
the Daalu cluster itself. Remove everything with `make down`.

### 2b. On your own cluster

1. **Build and push the images** to a registry the cluster can pull from:

   ```bash
   REG=registry.example.com/daalu TAG=v0.1.0
   docker build -t $REG/daalu-automation:$TAG .
   docker build -t $REG/daalu-automation-frontend:$TAG frontend
   docker build -t $REG/daalu-wg-hub:$TAG deploy/wg-hub
   docker push $REG/daalu-automation:$TAG
   docker push $REG/daalu-automation-frontend:$TAG
   docker push $REG/daalu-wg-hub:$TAG
   ```

2. **Prepare the cluster:** an ingress controller, a DNS name for Daalu pointing at
   it, and (for TLS) cert-manager with a ClusterIssuer. Label the node that will
   run the WireGuard hub and open UDP 51820 to it:

   ```bash
   kubectl label node <node> daalu.io/wireguard-hub=true
   ```

3. **Create the app Secret** from a production `.env` (real `SECRET_KEY` and
   `SERVICE_TOKEN_SECRET_KEY`, your model keys, and the hub endpoint):

   ```bash
   # in .env:
   #   WIREGUARD_PUBLIC_ENDPOINT=wg.example.com      # resolves to the hub node
   #   OPERATOR_API_PUBLIC_URL=https://daalu.example.com
   #   DATABASE_URL / DATABASE_SYNC_URL / REDIS_URL / CELERY_*  → in-cluster services
   kubectl create ns daalu-automation
   kubectl -n daalu-automation create secret generic daalu-automation-secrets --from-env-file=.env
   ```

   `deploy/scripts/install.sh` automates this step and the Helm install: it
   rewrites the `localhost` database URLs to the in-cluster services and seeds the
   hub key on first install (TLS via a cert-manager `letsencrypt-prod` issuer):
   `REGISTRY=$REG IMAGE_TAG=$TAG HOST=daalu.example.com ./deploy/scripts/install.sh`.

4. **Install the chart:**

   ```bash
   helm upgrade --install daalu-automation deploy/helm/daalu-automation -n daalu-automation \
     --set image.registry=$REG \
     --set image.backend.tag=$TAG --set image.frontend.tag=$TAG \
     --set wireguard.image=$REG/daalu-wg-hub:$TAG \
     --set wireguard.hubPrivateKey=$(python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())') \
     --set ingress.host=daalu.example.com \
     --set ingress.className=nginx \
     --set networkPolicies.ingressNamespace=ingress-nginx \
     --set 'config.corsOrigins=["https://daalu.example.com"]'
   ```

   Private registry: create a docker-registry Secret and add
   `--set image.pullSecret=<name>`. No cert-manager: add
   `--set ingress.tlsEnabled=false`. On k3s with Traefik, use
   `ingress.className=traefik` and leave `networkPolicies.ingressNamespace` at
   its default (`kube-system`).

5. **Create the admin login:**

   ```bash
   kubectl -n daalu-automation exec deploy/daalu-api -- daalu seed
   kubectl -n daalu-automation exec -it deploy/daalu-api -- daalu create-admin
   ```

The chart's [README](../deploy/helm/daalu-automation/README.md) lists every value.
For SSO with Keycloak/OIDC see the chart values `config.authMode` and
`config.keycloak*`. For a GitOps install, see [`deploy/argocd/`](../deploy/argocd).

---

## 3. Add a cluster for Daalu to manage

Daalu manages clusters through **daalu-edge**, a small Helm chart you install in
the target cluster. The edge dials the WireGuard hub (outbound UDP only, so the
cluster needs no inbound ports), gets a tunnel address such as `10.200.0.2`, and
exposes the cluster's API server (`:6443`) and an HTTP proxy for in-cluster
Prometheus and Loki (`:8888`) on that address.

The [`demo/`](../demo/README.md) folder builds a cluster worth managing: a kind
cluster `daalu-demo` with Prometheus, Alertmanager, Grafana, Loki and two apps
you can break on purpose. It's a separate cluster from Daalu (in Method 2 it's a
second kind cluster).

Set `DAALU_API` to Daalu's API URL:

| Method | `DAALU_API` |
|---|---|
| 1 (Compose) | `http://localhost:8000` (the default) |
| 2a (kind) | `http://localhost:8080` |
| 2b (your cluster) | `https://daalu.example.com` |

### Option A: onboard it from the UI (shows the whole flow)

1. Build the cluster and its monitoring, without connecting it:

   ```bash
   SKIP_ONBOARD=1 ./demo/up.sh
   ```

2. In Daalu, open **Managed infra → Clusters & observability → Onboard cluster**.
   Enter the slug `daalu-demo` and a display name, then submit. Daalu allocates a
   tunnel address and shows a one-time `helm install daalu-edge …` command.
3. Run that command against the demo cluster:

   ```bash
   kubectl config use-context kind-daalu-demo
   # paste the helm install command from the UI
   ```

4. Within about 30 seconds the cluster shows **Connected** in the clusters list.
5. Connect the cluster's Kubernetes API, Prometheus and Loki to Daalu. Create a
   personal access token (**Settings → API tokens**) and re-run the demo script.
   It sees the connected `daalu-demo` cluster, skips the parts already done, and
   registers the three integrations bound to it:

   ```bash
   DAALU_TOKEN=dpat_... ./demo/up.sh
   ```

   To do it by hand instead, use **Connect** in the Observability section and
   pick the `daalu-demo` cluster. In-cluster URLs:
   `http://prometheus-operated.monitoring.svc.cluster.local:9090` (Prometheus) and
   `http://loki.monitoring.svc.cluster.local:3100` (Loki).

### Option B: one command

Create a personal access token in **Settings → API tokens**, then:

```bash
export DAALU_TOKEN=dpat_...           # the token you just created
./demo/up.sh                          # DAALU_API=... if not using Compose
```

The script does everything in Option A for you: builds the cluster, calls the
same onboarding API the UI uses, runs the `helm install daalu-edge` command it
returns, waits for the tunnel, and registers the Prometheus, Loki and Kubernetes
integrations.

### Option C: a cluster you already have

Any Kubernetes cluster that can reach `DAALU_HOST_IP` on UDP 51820 works, including
one on the same machine as Daalu (k3s, kind, …). It needs no inbound ports.

1. In Daalu, open **Managed infra → Clusters & observability → Onboard cluster**,
   enter a slug (e.g. `lab`) and a display name, and submit. Copy the
   `helm install daalu-edge …` command it shows: its bootstrap token is shown once.
2. Run it against your cluster. Installing the edge in its own namespace keeps it
   apart from your workloads (and lets fault-injection tools protect it):

   ```bash
   kubectl create namespace daalu-edge
   # paste the command, adding -n daalu-edge after `helm install daalu-edge`
   ```

3. Within about 30 seconds the cluster shows **Connected**. The edge registers the
   cluster's Kubernetes API with Daalu by itself.
4. Connect the cluster's Prometheus: in **Observability → Prometheus / Alertmanager →
   Connect**, tick **Reachable via federation tunnel**, pick the cluster, and enter its
   in-cluster URL, for example
   `http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090` for a
   kube-prometheus-stack release of that name in `monitoring`
   (`kubectl get svc -A | grep prometheus` shows yours), then **Test** and **Save**.
   Prometheus alerting rules you add to the cluster now reach Daalu as alerts. Loki,
   if you run it, connects the same way.

### Check the tunnel

- In the UI: **Managed infra → Clusters & observability** lists the cluster as Connected.
- From Daalu's side:

  ```bash
  # Method 1
  docker compose exec wg-hub wg show
  # Method 2a
  kubectl -n daalu-automation exec deploy/daalu-wireguard-hub -- wg show
  ```

  A `latest handshake` line under the peer means the tunnel is up.

---

## 4. Run Daalu's AI on your own GPU (optional)

If the cluster you connected has an NVIDIA GPU, Daalu can run its AI there instead
of on a hosted model: it deploys an open model (Qwen3.6-35B-A3B on a 48 GB card,
Llama 3.1 8B on a 16 GB one) with vLLM, downloads the weights, and routes its own
requests to it over the tunnel. Prompts and logs stay on your hardware.

1. **The cluster needs the NVIDIA GPU Operator.** If
   `kubectl get nodes -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}'`
   prints nothing, install it with
   [Installing the NVIDIA GPU Operator](user-guide/06-advanced-topics/35-local-gpu-skus.md#installing-the-nvidia-gpu-operator)
   (it covers k3s's extra settings and the GPU metrics AI Factory shows).
2. **AI Factory → Add GPU**: pick the cluster, click the detected GPU node, keep the
   suggested model, **Deploy & connect**. Expand **Deploy progress** to watch the image
   pull, the weight download and the first start; the card turns **Online** when the
   model answers.

The full walkthrough, with what to expect at each step and troubleshooting, is
[chapter 35](user-guide/06-advanced-topics/35-local-gpu-skus.md) of the User Guide.

---

## 5. Watch the agent find and fix a problem

With the demo cluster from Option A or B:

```bash
./demo/break.sh            # bad-image (default) | crashloop | errors
./demo/status.sh           # app + alert state
```

`break.sh` pushes a bad rollout to `dummy-app`. Within about two minutes the
`DummyAppDown` alert fires in the demo cluster's Alertmanager, Daalu ingests it,
and it appears under **Alerts**. Open it: the agent inspects the workload through
the tunnel (rollout history, pod events, logs), names the bad rollout as the root
cause, and proposes a **rollback** as a pending change proposal. Nothing changes
until you approve it. Approve, and the app recovers.

To fix it by hand instead: `kubectl --context kind-daalu-demo -n daalu-demo rollout undo deploy/dummy-app`.

---

## 6. Tear down

```bash
DAALU_TOKEN=dpat_... ./demo/down.sh   # delete the demo cluster and detach it from Daalu
docker compose down -v                # Method 1: stop Daalu and delete its data
make down                             # Method 2a: delete the daalu-dev kind cluster
```

Without `DAALU_TOKEN`, `down.sh` still deletes the cluster; remove it from
**Managed infra** in the UI afterwards.

---

## Running from source (for development)

To change code with live reload, run the processes on the host and only Postgres
and Redis in Docker:

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

docker run -d --name daalu-postgres -e POSTGRES_DB=daalu_automation \
  -e POSTGRES_USER=daalu -e POSTGRES_PASSWORD=daalu_password \
  -p 5432:5432 postgres:16-alpine
docker run -d --name daalu-redis -p 6379:6379 redis:7-alpine

daalu migrate                 # create the schema (fresh DB) or upgrade it
daalu seed                    # default tenant
daalu create-admin            # your login
daalu seed-demo               # optional: synthetic events for the feed

daalu server --reload         # terminal 1: API on :8000
daalu worker                  # terminal 2: Celery worker
daalu beat                    # terminal 3: scheduler
daalu agents                  # terminal 4: agent host

cd frontend && cp .env.local.example .env.local
npm ci && npm run dev         # terminal 5: UI on :3000
```

Managed clusters need the WireGuard hub, so use Method 1 or 2 when you work on
cluster features.

---

## Troubleshooting

- **`migrate` exits with an error** (`docker compose ps` shows `exited (1)`):
  `docker compose logs migrate`. `ADMIN_PASSWORD` must be set in `.env`.
- **"Onboard cluster" says onboarding isn't configured:** `DAALU_HOST_IP` is empty.
  Set it in `.env` and run `docker compose up -d` again.
- **Cluster stays "Awaiting handshake":** the edge can't reach UDP 51820 on
  `DAALU_HOST_IP`. Check the address is right (`hostname -I`), that a host
  firewall allows UDP 51820, and the edge logs:
  `kubectl --context kind-daalu-demo logs deploy/daalu-edge -c edge`.
- **Edge pod `CrashLoopBackOff` with "bootstrap callback failed":** the edge can't
  reach the API at `http://DAALU_HOST_IP:8000` (Method 1) or `:8080` (Method 2a).
  `curl http://<DAALU_HOST_IP>:8000/health` from the host should return `ok`.
- **No alerts appear:** check the Prometheus integration is bound to the cluster
  (Integrations page) and the worker logs: `docker compose logs -f worker`.
- **"No LLM credentials configured" / triage is generic:** set a model in `.env`
  (see Prerequisites) and run `docker compose up -d` (or re-run `make bootstrap-dev`).
- **Port already in use:** another service holds 3000/8000/8080/51820. Stop it, or
  change the published port in `docker-compose.yml`. For the kind install, run
  `DAALU_HTTP_PORT=18080 make bootstrap-dev` (and use that port in `DAALU_API`).
- **kube-proxy or other pods in a kind cluster crash with "too many open files":**
  raise the inotify limits (see [Prerequisites](#0-prerequisites)), then delete the
  failed pods or recreate the cluster.
- **Monitoring pods Pending or OOMKilled in the demo cluster:** give Docker more
  memory; the monitoring stack is the heaviest part.
