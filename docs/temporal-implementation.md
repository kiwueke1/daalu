# Temporal-driven Daalu workflows

This document describes how the Temporal control plane is wired into Daalu —
what was added, why, and how to use it. After `daalu mgmt` finishes, an
operator can open the temporal-console UI and run the rest of the cloud
build (provision workload nodes, deploy Ceph, OpenStack, etc.) by clicking
buttons rather than typing CLI commands. Output is streamed back into the UI
as a collapsible per-stage view.

---

## 1. Goals

| | |
|---|---|
| **Self-serve operations** | Anyone with access to the workflows UI can run the same provisioning steps the lead operator would otherwise drive from a terminal. |
| **Replayable history** | Every execution lands in Temporal's durable history — re-runs, failures, retries, and timing are queryable forever. |
| **No CLI rewrite** | Activities shell out to the existing `daalu` CLI. Workflow logic adds orchestration only; deploy logic stays where it already lives. |
| **Per-stage observability** | Each stage shows its status, duration, and full log. The currently-running stage shows a live tail without the worker pushing per-line events into history. |

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                  Management Kubernetes cluster                            │
│                                                                          │
│  ┌─────────────────────┐    ┌────────────────────┐    ┌──────────────┐  │
│  │  Temporal server    │◄──►│  daalu-worker      │    │ temporal-    │  │
│  │  (helm: temporalio/ │    │  (this repo —      │◄──►│ console      │  │
│  │   temporal)         │    │  src/daalu/        │    │ (Phase 1+2   │  │
│  │                     │    │   temporal/)       │    │  in this PR) │  │
│  │  ns: temporal       │    │  ns: daalu         │    │ ns: daalu    │  │
│  └─────────────────────┘    └────────────────────┘    └──────────────┘  │
│           ▲                          │                       ▲           │
│           │ start workflow           │ subprocess:           │ HTTP +    │
│           │ + signals                │  daalu deploy         │ HTMX      │
│           │                          │  daalu clean          │           │
│           │                          ▼                       │           │
│           │                ┌────────────────────┐            │           │
│           │                │ workspace mount    │            │           │
│           │                │  cluster-defs/     │            │           │
│           │                │  helm-charts/      │            │           │
│           │                │  assets/           │            │           │
│           │                │  cloud-config/     │            │           │
│           │                └────────────────────┘            │           │
│           └──────────────────────────────────────────────────┘           │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
                                     ▲
                                     │  https://workflows.daalu.io
                                     │
                                ┌────┴────┐
                                │ Operator│
                                │ browser │
                                └─────────┘
```

### Component map

| Piece | Lives at | Responsibility |
|---|---|---|
| Workflows | `src/daalu/temporal/workflows.py` | Orchestrate stages, expose `progress` query. |
| Activities | `src/daalu/temporal/activities.py` | Spawn `daalu deploy --install <stage>` as subprocess, heartbeat tail. |
| Models | `src/daalu/temporal/models.py` | `DeployRequest`, `CleanRequest`, `MirrorImagesRequest`, `StageInput`, `StageResult`, `WorkflowProgress`. |
| Schemas | `src/daalu/temporal/schemas.py` | Workflow input schemas — drives the UI form. |
| Worker entry | `src/daalu/temporal/worker.py` | `daalu-worker` console entry point. |
| Registry CLI | `src/daalu/temporal/cli.py` | `daalu-registry` — dumps schemas as JSON. |
| Worker chart | `deployments/daalu-worker/` | Dockerfile + first-party helm chart for the worker deployment. |
| Console source + chart | `external/temporal-console/` | Full FastAPI UI app (Dockerfile, chart, source) shipped in-tree so a single clone has everything. |
| Server chart (user-pulled) | `assets/temporal/charts/temporal/` | Pulled from `temporal/temporal` by the operator, same pattern as MetalLB / Ingress. See README. |
| Server installer | `src/daalu/bootstrap/mgmt/temporal_installer.py` | `helm install` Temporal server, daalu-worker, temporal-console. |
| Server config | `src/daalu/bootstrap/mgmt/models.py` (`TemporalConfig`) | `mgmt_cluster.temporal.*` keys in `cluster.yaml`. |
| Console — registry | `external/temporal-console/src/temporal_console/registry.py` | Loads `registry.json` baked into the image. |
| Console — start router | `external/temporal-console/src/temporal_console/api/routers/start.py` | Renders typed form, posts to `client.start_workflow`. |
| Console — progress | `external/temporal-console/src/temporal_console/api/routers/workflows.py` (`/workflows/{id}/progress.html`) | HTMX partial — reads `progress` query + pending-activity heartbeat. |
| Console — templates | `external/temporal-console/.../templates/workflow_start_*.j2`, `_partials/workflow_progress.html.j2` | Pick + form + collapsible step view. |

---

## 3. The streaming model

This is the part that took the most thought. The constraint:

> A Temporal workflow CANNOT read its own running activity's heartbeat
> details from inside workflow code.

Heartbeat details are stored on the Temporal server, not pushed back into
the workflow loop. The official patterns for "live progress" are:

1. **Activity → workflow signals** — one signal per chunk. Floods history.
2. **Activity returns final result** — fine for completed stages, no live view.
3. **External observer reads `describe_workflow_execution`** — the SDK lets
   any client query a running workflow's pending activities and read
   their last heartbeat detail. This is what we use for the live tail.

Concretely:

| State | What the UI sees | Source |
|---|---|---|
| Stage finished (success or fail) | Status + full log tail | `progress` query → `WorkflowProgress.stages[i].log_lines` (workflow stored the activity's `StageResult.log_tail`). |
| Stage currently running | Status + live rolling tail (~200 lines, updated ~1 Hz) | `client.get_workflow_handle(id).describe()` → `pending_activities[0].heartbeat_details[-1]["tail"]`. |
| Stage not started | "pending" pill, empty | `progress` query — entry exists but `started_at` is null. |

The activity heartbeats once per second with the latest tail (not per
line). One heartbeat = one Temporal history event = one stored detail
snapshot. Even a 30-minute provisioning step writes ~1800 history events,
well under the 50k/event soft cap.

```
                ┌──────────────────────────────────────┐
                │    daalu-worker (activity thread)    │
                │                                      │
                │   subprocess.Popen(['daalu', ...])   │
                │            │                         │
                │            ▼  stdout line by line    │
                │   tail = deque(maxlen=200)           │
                │   tail.append(line)                  │
                │                                      │
                │   if (now - last_heartbeat) > 1s:    │
                │     activity.heartbeat({             │
                │       "tail": list(tail),            │
                │       "n":    line_count,            │
                │     })                               │
                └──────────────────────────────────────┘
                              │
                  Temporal server stores latest
                              │
                              ▼
                ┌──────────────────────────────────────┐
                │       temporal-console (UI)          │
                │                                      │
                │   GET /workflows/{id}/progress.html  │
                │     (HTMX poll every 2s)             │
                │                                      │
                │   ┌───────────────────────────┐      │
                │   │ handle.query("progress")  │ ◄─── workflow's structured state
                │   │ handle.describe()         │ ◄─── live heartbeat tail
                │   └───────────────────────────┘      │
                │                                      │
                │   render collapsible cards           │
                └──────────────────────────────────────┘
```

---

## 4. Adding Temporal to `daalu mgmt`

`MgmtClusterManager.deploy()` runs **TemporalInstaller** after Harbor:

1. **Server** — `helm upgrade --install temporal temporalio/temporal` in
   namespace `temporal`. The bundled Postgres backend is good enough for a
   single-operator mgmt cluster. To switch to Cassandra/MySQL or external
   storage, set `mgmt_cluster.temporal.storage` and override values via the
   helm chart.
2. **Worker** — `helm upgrade --install daalu-worker
   deployments/daalu-worker/chart` in namespace `daalu`. The pod
   `hostPath`-mounts the daalu workspace, the mgmt kubeconfig, and `~/.ssh/`
   from the mgmt node. Those are exactly the resources the operator's CLI
   already uses, so the worker's `daalu` runs see the same world.
3. **Console** — `helm upgrade --install temporal-console <path>` in
   namespace `daalu`. Picks up the chart from `../temporal-console/chart`
   relative to the workspace root, falling back to
   `../daalu_private/temporal-console/chart`.

### Configuration

```yaml
# cluster-defs/cluster.yaml

mgmt_cluster:
  host: "192.168.0.171"
  # … usual mgmt fields …
  install_harbor: true

  temporal:
    enabled: true                   # set false to skip everything below
    namespace: temporal
    server_chart_path: assets/temporal/charts/temporal
    server_image_tag: "1.27.0"
    storage: postgresql             # postgresql | mysql | cassandra

    worker_namespace: daalu
    worker_chart_path: deployments/daalu-worker/chart
    worker_image: "10.10.0.9:30003/daalu/daalu-worker:latest"
    worker_replicas: 1
    worker_threads: 4

    console_enabled: true
    console_namespace: daalu
    console_chart_path: external/temporal-console/chart
    console_image: "10.10.0.9:30003/daalu/temporal-console:latest"
    console_brand_name: "Daalu Workflows"
    console_host: "workflows.daalu.io"
    console_oidc_issuer: "https://auth.daalu.io/realms/daalu"
    console_oidc_client_id: "temporal-console"
```

`temporal.enabled: false` skips the whole subsystem cleanly — daalu CLI
still works without Temporal.

### Pulling the Temporal helm chart

The Temporal server chart is third-party, so it follows the same pattern
as MetalLB / Ingress / Keycloak — the operator pulls it once before
running `daalu mgmt`:

```bash
helm repo add temporal https://go.temporal.io/helm-charts
helm pull temporal/temporal --untar --untardir assets/temporal/charts/
```

The `daalu-worker` and `temporal-console` charts are first-party (live in
this repo at `deployments/`) so no pull is needed for those.

### Building the worker image

The worker image (`10.10.0.9:30003/daalu/daalu-worker:latest`) ships
the `daalu` package and the CLI tools it shells out to (`kubectl`, `helm`,
`clusterctl`, `cilium`, `ssh`).

```bash
docker build -f deployments/daalu-worker/Dockerfile -t 10.10.0.9:30003/daalu/daalu-worker:latest .
docker push 10.10.0.9:30003/daalu/daalu-worker:latest
```

### Building the console image

The temporal-console UI source ships in this repo at
`external/temporal-console/`. The Daalu workflow registry is baked into
the image at `src/temporal_console/registry.json`. To refresh it from the
daalu source and rebuild:

```bash
cd /path/to/daalu

# Refresh the workflow registry from daalu's schemas
daalu-registry > external/temporal-console/src/temporal_console/registry.json

# Build & push
docker build \
  -t 10.10.0.9:30003/daalu/temporal-console:latest \
  external/temporal-console
docker push 10.10.0.9:30003/daalu/temporal-console:latest
```

You can also override the registry at runtime by mounting a configmap and
setting `REGISTRY_FILE=/etc/daalu/registry.json` on the console pod.

---

## 5. The user flow

### Step 1 — Bootstrap the mgmt cluster (CLI)

```bash
daalu mgmt cluster-defs/cluster.yaml
```

When this finishes you'll have:

* `~/.kube/daalu-mgmt-config` — kubeconfig for the mgmt cluster
* Harbor at `https://10.10.0.9:30003`
* Temporal frontend at `temporal-frontend.temporal.svc.cluster.local:7233`
* daalu-worker pod in `daalu/` polling task queue `daalu.deployments`
* temporal-console at `https://workflows.daalu.io`

### Step 2 — Deploy the workload cloud (UI)

1. Open `https://workflows.daalu.io` and authenticate via Keycloak.
2. **Start workflow** in the sidebar → tile grid of registered workflows
   (Deploy Daalu Cloud, Tear down Daalu Cloud, Mirror images).
3. Click **Deploy Daalu Cloud** → typed form. Required fields are marked
   with `*`. Advanced fields (registry URL, kube context, `--phase`,
   `--dry-run`, etc.) are folded under **Advanced options**.
4. Click **Start workflow**. You're redirected to
   `/workflows/<id>` — the workflow detail page.
5. The **Pipeline** card refreshes every 2 s. Each stage is a collapsible
   row:
   * `pending` (grey) — not started
   * `running` (animated dot) — open by default; tail of stdout updates
     every ~1 s
   * `succeeded` (green check) — collapsed; expand to see the full log tail
   * `failed` (red ×) — open by default with the error and final log
6. The **History timeline** below shows the raw Temporal events for
   debugging.

### Step 3 — Iteration

Re-run a single failed stage:

1. From the detail page note which stage failed.
2. **Start workflow** → **Deploy Daalu Cloud** → set `install` to just that
   stage (e.g. `infrastructure`).
3. Submit. The workflow runs only that stage and updates the cluster.

This works because the underlying CLI already supports `--install
<stage>` and each stage is idempotent; the activity layer adds nothing
beyond plumbing.

---

## 6. Adding a new workflow

Three places to edit, all in this repo:

1. **Add a request dataclass** in `src/daalu/temporal/models.py`.
2. **Write the workflow + activity** in
   `src/daalu/temporal/workflows.py` and `activities.py`. Append to
   `ALL_WORKFLOWS` and `ALL_ACTIVITIES`. The activity should call the same
   `_run_streaming(...)` helper so the live tail comes for free.
3. **Add a schema** in `src/daalu/temporal/schemas.py` and append it to
   `REGISTRY`. The fields you list become form fields in the console.

After deploying a new worker image, re-run `daalu-registry > registry.json`
and rebuild the console (or hot-reload via `REGISTRY_FILE`).

The worker's task queue is `daalu.deployments` for everything in this
repo; pick a different `task_queue` in the schema if you want a separate
worker pool.

---

## 7. Operations

### Tail worker logs

```bash
kubectl -n daalu logs -f deploy/daalu-worker
```

### List Temporal workflows from CLI (no UI)

```bash
kubectl -n temporal port-forward svc/temporal-frontend 7233:7233 &
temporal --address localhost:7233 workflow list
```

### Re-deploy the worker

```bash
helm upgrade daalu-worker deployments/daalu-worker/chart \
  -n daalu \
  --reuse-values \
  --set image.tag=$(git rev-parse --short HEAD)
```

The chart sets `strategy: Recreate`, so we never have two workers polling
the same task queue with different code versions during a rolling update.

### Reset Temporal state (dangerous)

The bundled Postgres in the helm chart uses an emptyDir volume by default,
so a `helm uninstall temporal -n temporal && helm install …` wipes
history. For persistence, set `postgresql.primary.persistence.enabled=true`
plus a storage class.

### Manually trigger a workflow from CLI

```bash
temporal --address temporal-frontend.temporal.svc.cluster.local:7233 \
  workflow start \
  --type DeployDaaluCloud \
  --task-queue daalu.deployments \
  --workflow-id manual-$(date +%s) \
  --input '{"config_path":"cluster-defs/cluster.yaml","install":"infrastructure"}'
```

### Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Workflow stays in `RUNNING` but no activity scheduled | Worker not reachable / wrong task queue | `kubectl -n daalu logs deploy/daalu-worker` — confirm "ready task_queue=daalu.deployments". |
| Activity fails immediately with `daalu: command not found` | Worker image rebuilt without re-installing the package | Rebuild image; the Dockerfile installs in editable mode but expects `pyproject.toml` to define the `daalu` script entry. |
| Live tail empty for a running stage | `daalu` buffering stdout | The activity sets `PYTHONUNBUFFERED=1` and `TERM=dumb`; if a downstream tool buffers, prefer line-oriented output (e.g. set `--no-progress` flags). |
| `progress` query returns null fields | The workflow type isn't one of ours | Custom workflows that don't expose `progress` fall back to the "no structured progress" message. The history timeline below the card still works. |

---

## 8. Why subprocess instead of in-process?

The activities shell out to `daalu deploy --install <stage>` rather than
importing daalu helpers and calling them directly. The trade-off:

| | Subprocess (current) | In-process import |
|---|---|---|
| Refactor cost | None — daalu's CLI is the orchestration boundary already. | Significant — each helper currently expects a shared SSH client, helm runner, etc., constructed at top-level in the `deploy` Typer command. |
| Output streaming | Trivial — capture stdout. Matches what a CLI user sees. | Requires installing a custom log handler that pipes into the heartbeat. |
| Idempotency | Each stage is its own process; failures don't poison shared state. | Bugs in one stage's cleanup can leak across stages. |
| SSH overhead | Each stage re-establishes SSH (~1–2 s). | Reuses one connection. |
| Crash isolation | OS-level. Worker survives any subprocess crash. | A C-extension SEGV would kill the worker. |

For our use case (long-running stages where the SSH setup cost is
~negligible), subprocess is the clearly better choice. If we later want
to optimize, the activities can be converted in place without changing the
workflow API or UI — they can return the same `StageResult`.
