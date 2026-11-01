# 91. The Docker Compose install (open-source distribution)

Chapters 42–46 cover how *we* run Daalu: a source checkout with Compose-managed
dependencies for development (42), and the Helm chart rolled out by
`install.sh` / Argo CD for production (43–46). This chapter covers the third
way, which only exists in the **open-source distribution** (this repository,
`github.com/kiwueke1/daalu`): a one-machine install with `docker compose up`,
where the WireGuard hub runs as a plain container and managed clusters still
join over the tunnel exactly as they do under Kubernetes.

> **Where the code lives.** References below are paths in this repository. The
> user-facing walkthrough is `docs/getting-started.md` (Method 1); this chapter
> explains *why* the Compose file looks the way it does.

| Piece | Path |
|---|---|
| Compose file | `docker-compose.yml` (213 lines, header comment at 1–17) |
| File-backed hub settings | `src/daalu_automation/config.py:578-586` (`wireguard_hub_config_path`, `wireguard_hub_dump_path`) |
| File-mode coordinator | `src/daalu_automation/core/wireguard.py:272-324` (`hub_config_file`, `_ensure_hub_config_file`, `_write_hub_config_file`), file branch of `sync_hub_secret` at 354-385 |
| Hub container | `deploy/wg-hub/Dockerfile`, `deploy/wg-hub/entrypoint.sh` (`WG_DUMP_PATH`, `write_dump` at 33-42) |
| Health task, file mode | `src/daalu_automation/workers/wireguard_health.py:119-130` |
| Fresh-DB bootstrap | `src/daalu_automation/cli.py:392-455` (`daalu migrate`) |
| User guide | `docs/getting-started.md` (§1 Method 1, §3 add a cluster, Troubleshooting) |
| Env template | `.env.example:1-8` (the Compose block: `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `DAALU_HOST_IP`) |

The change that introduced all of this is commit `302139a` ("Add
Docker Compose quick start and fix the kind install").

---

## 91.1 What runs

`docker compose up -d --build` builds three images — the backend
(`daalu-automation:local`, from the repo `Dockerfile`), the web UI
(`daalu-automation-frontend:local`, from `frontend/`) and the hub
(`daalu-wg-hub:local`, from `deploy/wg-hub/`) — then starts:

| Service | Command | Network | Notes |
|---|---|---|---|
| `postgres` | `postgres:16-alpine` | default | data in the `pgdata` volume; `pg_isready` healthcheck |
| `redis` | `redis:7-alpine` | default | `redis-cli ping` healthcheck |
| `migrate` | `daalu migrate && daalu seed && daalu create-admin …` | default | one-shot, runs on **every** `up` (§91.4) |
| `net` | `registry.k8s.io/pause:3.10` | default, alias `api` | owns the shared network namespace; publishes 8000/tcp and 51820/udp (§91.2) |
| `wg-hub` | `/entrypoint.sh` | `service:net` | `NET_ADMIN`, `NET_RAW`; `wireguard` volume at `/etc/wireguard` |
| `api` | `daalu server --host 0.0.0.0 --port 8000` | `service:net` | `wireguard` volume; `curl /health` healthcheck |
| `worker` | `daalu worker --concurrency 4` | `service:net` | `wireguard` volume (the tunnel-health task reads the dump) |
| `agents` | `daalu agents` | `service:net` | |
| `executor` | `daalu executor --concurrency 2` | `service:net` | `EXECUTOR_JWT_SCOPE=executor` (chapter 17) |
| `beat` | `daalu beat` | default | only schedules; does not need the tunnel |
| `frontend` | Next.js on :3000 | default | built with `NEXT_PUBLIC_API_BASE_URL=http://api:8000` |

All backend services share the `x-backend` anchor
(`docker-compose.yml:19-31`): same image, `.env` loaded if present,
`restart: unless-stopped`, image healthcheck disabled, and
`depends_on: migrate: service_completed_successfully` — nothing backend starts
until the schema exists.

### One `.env` for Compose and for running from source

The `x-backend-env` block (`docker-compose.yml:33-49`) overrides the
`localhost` database / Redis / Celery URLs from `.env` with container-network
addresses (`postgres:5432`, `redis:6379`). The same `.env` therefore works both
for Compose and for running the processes on the host (the "Running from source"
section of the guide). It also forces `AUTH_COOKIE_SECURE=false` (plain HTTP on
localhost) and builds `CORS_ORIGINS` from `localhost:3000` plus
`${DAALU_HOST_IP}:3000`.

The frontend container reaches the API at `http://api:8000`. `api` is not the
`api` service's name on the network — a service in `network_mode: service:net`
has no network identity of its own — it is an **alias of the `net` service**
(`docker-compose.yml:116-118`). The URL is baked in at build time via the
`NEXT_PUBLIC_API_BASE_URL` build arg (`frontend/Dockerfile`), which
the Next.js rewrites use for `/api`, `/sse`, `/ide` and `/ws`. Under Kubernetes
the ingress routes those paths straight to the API, so the default
(`http://localhost:8000`) only matters for Compose.

---

## 91.2 The shared network container

The services that must reach managed clusters — `api`, `worker`, `agents`,
`executor` — and the hub itself all run in **one network namespace owned by a
`pause` container** (`net`, `docker-compose.yml:104-118`). This is the same
trick a Kubernetes pod uses: the pause container holds the namespace, and the
real containers join it.

Why they need to share it: `wg-hub` creates the `wg0` interface with address
`10.200.0.1/16`. Any process in the same namespace gets a route to the
`10.200.0.0/16` tunnel mesh for free, so the API can dial a managed cluster's
apiserver at `https://10.200.0.2:6443` and its edge HTTP proxy at
`http://10.200.0.2:8888` with no extra routing, NAT rules or sidecars — the
same addresses the Kubernetes install uses (chapter 27). Note that the
file-mode `wg0.conf` (rendered by the same `render_hub_config` as the Secret
mode) carries no `PostUp` routing rules; none are needed here.

Consequences of the design, all visible in the file:

- **Ports are published on `net`, not on `api` or `wg-hub`.** A container in
  `network_mode: service:…` cannot publish ports. `net` publishes `8000:8000`
  (the API) and the hub's `51820/udp`.
- **Restarting one service leaves the others' networking intact**, because the
  namespace belongs to `net`, which has nothing to crash and restarts only with
  the stack.
- **`wg-hub` cleans up after itself on start.** A crashed hub (no TERM, so no
  `wg-quick down`) leaves `wg0` behind in a namespace that outlives the
  container — the shared netns under Compose, the host under `hostNetwork` in
  Kubernetes. The entrypoint runs `ip link del wg0` before `wg-quick up`
  (`deploy/wg-hub/entrypoint.sh:55-59`) so the restart doesn't fail with
  "interface exists".

**The rejected alternative** was to put the backend services in
`network_mode: service:wg-hub` directly. That works until the hub restarts:
the namespace goes with it, and every service that joined it loses its
networking. Commit `302139a` lists the fix explicitly ("Share one network
container between services so a hub restart no longer cuts off the API's
networking under Compose").

### Why UDP 51820 is bound to `DAALU_HOST_IP`

```yaml
- "${DAALU_HOST_IP:-0.0.0.0}:51820:51820/udp"   # docker-compose.yml:115
```

With a wildcard bind, Docker's userland UDP proxy answers a kind cluster on the
same host **from the bridge gateway address** instead of from the address the
edge dialled. The kind node's NAT doesn't recognise the reply as part of the
flow and drops it, so the cluster sits at "Awaiting handshake" forever. Binding
to `DAALU_HOST_IP` makes replies leave from the address the edge sent to. This
was found on the 2026-10-31 run (see the S0-02 episode notes) and fixed in the
same commit.

---

## 91.3 File-backed WireGuard hub mode

Under Kubernetes (chapter 24) the coordinator keeps `wg0.conf` in the
`daalu-wireguard-peers` Secret and the hub pod watches the mounted Secret. There
is no Kubernetes API under Compose, so this repository adds a second mode, selected
by one setting.

| Setting | Compose value | Meaning |
|---|---|---|
| `WIREGUARD_HUB_CONFIG_PATH` | `/etc/wireguard/wg0.conf` | When non-empty, the coordinator reads/writes this file instead of patching the Secret (`config.py:578-585`) |
| `WIREGUARD_HUB_DUMP_PATH` | `/etc/wireguard/wg0.dump` | Where the health task reads `wg show wg0 dump` instead of exec'ing into the hub pod (`config.py:586`) |
| `WG_DUMP_PATH` (hub container) | `/etc/wireguard/wg0.dump` | Tells the hub entrypoint to write the dump every tick (`entrypoint.sh:33-42`) |

The `wireguard` named volume is mounted at `/etc/wireguard` in `wg-hub`, `api`
and `worker`. The hub's entrypoint is wrapped in
`chown 1000:1000 /etc/wireguard && exec /entrypoint.sh`
(`docker-compose.yml:130-133`) because the API runs as uid 1000 and must be able
to write the config into the volume.

### Writing the config (`core/wireguard.py`)

- `hub_config_file()` (`wireguard.py:272`) returns the path, or `None` for
  Secret mode. Every caller branches on it.
- **Seeding the hub key.** The Helm path seeds the hub's private key at install
  time (`wireguard.hubPrivateKey`). Compose has no install step, so
  `_ensure_hub_config_file` (`wireguard.py:279-301`) generates a fresh keypair
  and writes a peerless config the first time anything needs it. "Anything" is
  whichever comes first: the tunnel-health task (which calls `sync_hub_secret`
  on every pass), the onboarding endpoint, or `_load_hub_pubkey` in
  `api/routers/clusters.py:201-213`, which seeds it so the very first install
  snippet carries a real hub public key instead of a placeholder.
- **Atomic writes.** `_write_hub_config_file` (`wireguard.py:304-324`) writes a
  temp file in the same directory, `chmod 600`, then `os.replace`s it. The rename
  lands as a `moved_to` event on the hub's directory watch, so the hub runs
  `wg syncconf` straight away rather than waiting for the 15 s backstop.
- **`sync_hub_secret` keeps its name** but, in file mode
  (`wireguard.py:354-385`), re-renders from the DB, preserves the file's existing
  `PrivateKey` (so a peer add never rotates the hub identity), and only writes
  when the rendered text differs.

### The hub side (`deploy/wg-hub/entrypoint.sh`)

The same entrypoint serves both modes. It waits for a non-empty `wg0.conf`
(this is what the hub container is doing between `up` and the first seed),
brings `wg0` up, then reconciles on two triggers: an `inotifywait` on the
**directory** (event-driven) and an unconditional `wg syncconf` every
`WG_RECONCILE_INTERVAL` (15 s) seconds. Chapter 24 §"The reload bug and the
fix" explains why the watch is on the directory; under Compose the atomic
rename is what triggers it. After every reconcile, `write_dump` writes
`wg show wg0 dump` to `$WG_DUMP_PATH` (tmp + `mv`) when that variable is set.

### Tunnel health without `kubectl exec`

Under Kubernetes the beat-scheduled health task execs `wg show wg0 dump` in the
hub pod. In file mode (`workers/wireguard_health.py:119-130`) it simply reads
the dump file — "there is no pod to exec into" — and then runs the same
handshake classification (connected / degraded windows) and the same
self-healing `sync_hub_secret` as in Kubernetes. That is why `worker` mounts the
`wireguard` volume and `beat` doesn't: beat only enqueues the task; the worker
runs it.

---

## 91.4 `daalu migrate` on a fresh database

The `migrate` service runs `daalu migrate && daalu seed && daalu create-admin`
(`docker-compose.yml:76-102`) on every `docker compose up`. All three are
idempotent:

- `daalu migrate` (`src/daalu_automation/cli.py:392-455`) checks for
  the `alembic_version` table. **Present** → `alembic upgrade head`. **Absent**
  (a fresh database) → import every model and module so they register on
  `Base.metadata`, `create_all`, then `alembic stamp head`. No incremental
  migrations run on a fresh database because there is nothing for them to
  alter.
- `daalu seed` creates the default tenant if missing and seeds the SKU catalog.
- `daalu create-admin` takes `ADMIN_EMAIL` / `ADMIN_PASSWORD`. Compose refuses
  to start without a password: `ADMIN_PASSWORD: ${ADMIN_PASSWORD:?set
  ADMIN_PASSWORD in .env}` (`docker-compose.yml:88`).

**Why not `alembic upgrade head`:** the oldest migration,
`0001_multi_tenancy`, starts with `op.add_column("tenants", …)` — it assumes a
schema created by an earlier `create_all`. On an empty database it fails. The
original open-source release's chart ran raw alembic and broke on a first
install; commit `302139a` uses `daalu migrate` for the new Compose file and
switched the chart to it as well
(Compose here; Helm in `deploy/helm/daalu-automation/templates/api.yaml:38-41`).
This repo has the same command (`src/daalu_automation/cli.py:392`), but its
chart's init container still runs raw `alembic upgrade head`
(`deploy/helm/daalu-automation/templates/api.yaml:35-38`) — fine for the
long-lived production database, which already has `alembic_version`; see
chapter 50.

`docker compose ps` therefore shows `migrate` as `exited (0)` on a healthy
stack. If it shows `exited (1)`, nothing else backend started:
`docker compose logs migrate`.

---

## 91.5 `DAALU_HOST_IP` — the one setting that matters for clusters

`DAALU_HOST_IP` is the address other machines (and a kind cluster on the same
host) reach this one on — the guide uses the first entry of `hostname -I`.
Compose turns it into two backend settings (`docker-compose.yml:46-49`):

| Backend setting | Value | Used for |
|---|---|---|
| `WIREGUARD_PUBLIC_ENDPOINT` | `${DAALU_HOST_IP}` | the `Endpoint` the edge dials (UDP 51820) |
| `OPERATOR_API_PUBLIC_URL` | `http://${DAALU_HOST_IP}:8000` (only if set) | the bootstrap callback URL baked into the edge install snippet |

It also sets the UDP bind address (§91.2) and the extra CORS origin.

**Empty is allowed** and leaves cluster onboarding disabled. The onboarding
endpoint returns 503 from `_require_endpoint` / `_require_api_public_url`
(`src/daalu_automation/api/routers/clusters.py:150-169`). Be aware
that the message text is written for Kubernetes — it says to set
`wireguard_public_endpoint` / `OPERATOR_API_PUBLIC_URL` "in the daalu-api
Secret" — so a Compose user has to know that means `DAALU_HOST_IP` in `.env`.
The guide's Troubleshooting section says so.

A wrong `DAALU_HOST_IP` fails in one of two ways, and the edge logs tell them
apart:

- edge pod `CrashLoopBackOff` with "bootstrap callback failed" → the edge can't
  reach `http://DAALU_HOST_IP:8000`;
- callback succeeds, cluster stuck at "Awaiting handshake" → UDP 51820 isn't
  reaching the hub (wrong address, host firewall, or the wildcard-bind problem
  above).

---

## 91.6 How this differs from the Helm install

| | Compose (this chapter) | Helm (chapters 44–45) |
|---|---|---|
| Hub config | file on a shared volume | `daalu-wireguard-peers` Secret |
| Hub key | generated on first use | `wireguard.hubPrivateKey` at install |
| Hub networking | shared `pause` netns with the backend | `hostNetwork` pod on a labelled node |
| Tunnel health | reads `wg0.dump` | `kubectl exec … wg show wg0 dump` |
| Schema | `migrate` one-shot service | API init container (`daalu migrate`) |
| TLS / ingress | none (`AUTH_COOKIE_SECURE=false`) | ingress + cert-manager |
| Scale | one container per service | replicas per values |

Compose is the evaluation path; the guide's comparison table says so ("Best
for: trying Daalu, demos, a laptop"). Things it does not attempt: TLS, SSO
(the chart's `config.authMode` / Keycloak values), image registries, or
running on macOS/Windows — the hub needs the Linux kernel's `wireguard`
module (5.6+).

---

## 91.7 What to verify before demoing it

- `docker compose ps`: every service `running`, `api` `healthy`, `migrate`
  `exited (0)`.
- `docker compose exec wg-hub cat /etc/wireguard/wg0.conf` has an `[Interface]`
  block (the key was seeded) and one `[Peer]` per onboarded cluster once its
  edge has called back.
- `docker compose exec wg-hub wg show` → a `latest handshake` line under each
  peer. `/etc/wireguard/wg0.dump` updates every ~15 s.
- `curl http://$DAALU_HOST_IP:8000/health` from the host returns `ok` (this is
  what the edge's bootstrap callback depends on).
- Restart a single service (`docker compose restart api`) and confirm the
  cluster stays Connected — that's the shared-namespace design doing its job.

Related chapters: [24 · The WireGuard hub](../05-cluster-federation/24-wireguard-hub.md),
[26 · The join flow](../05-cluster-federation/26-join-flow.md),
[27 · Cross-cluster API access](../05-cluster-federation/27-cross-cluster.md),
[42 · Local development setup](42-local-dev.md),
[44 · The Helm chart](44-helm-chart.md),
[50 · Database migrations in production](../09-operations-runbooks/50-prod-migrations.md).
