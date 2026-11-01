# 93. AIPerf load testing — the benchmark runner behind AI Factory

Chapters 30 and 80 get a model *serving*. This chapter covers how Daalu
**measures** a serving endpoint: an [AIPerf](https://github.com/ai-dynamo/aiperf)
concurrency sweep (TTFT, inter-token latency, throughput vs concurrency) that a
site operator or a GPU owner starts from **AI Factory → Performance
benchmarking**, and that the gpu-controller runs as a one-shot Kubernetes Job on
the cluster that hosts the endpoint.

AIPerf (`ai-dynamo/aiperf`, Apache-2.0, the successor to the deprecated
GenAI-Perf) is a pure OpenAI-compatible **client**. It needs no operator, CRD or
scheduler and changes nothing in the serving stack. That is why Daalu's whole
integration is a Job builder, an output parser and a queue table — the design
rationale is in the internal design notes (not published).

| Piece | Path |
|---|---|
| Job builder + parsers (pure, unit-tested) | `src/daalu_automation/gpu_controller/aiperf.py` |
| Run row (`aiperf_runs`) | `src/daalu_automation/models/aiperf_run.py`, migration `migrations/versions/0024_aiperf_runs.py` |
| Runner (reconcile loop) | `src/daalu_automation/gpu_controller/app.py:609` `_reconcile_aiperf`, `:658` `_run_one_aiperf` |
| API (`/ai-factory/aiperf/runs…`) | `src/daalu_automation/api/routers/gpu_metrics.py:585-818` |
| Artifact store client | `src/daalu_automation/core/object_storage.py:44` `aiperf_client` |
| Settings | `src/daalu_automation/config.py:218-251` (`gpu_aiperf_*`), `:118` `s3_bucket_aiperf` |
| UI | `frontend/app/ai-factory/benchmark/page.tsx`, `frontend/app/ai-factory/components/aiperf-panel.tsx` |
| Hand-run equivalents | `deploy/k8s/gpu/aiperf-bench-job.yaml`, `scripts/aiperf-bench.sh` |
| Tests | `tests/test_aiperf.py` |

---

## 93.1 The shape: queue row → controller Job → parsed summary

AIPerf follows the same split as the GPU diagnostics in chapter 95: **daalu-api
never touches a cluster.** The API writes a `pending` row; the gpu-controller —
the only component holding Kubernetes credentials for GPU clusters — picks it
up on its 30 s reconcile tick (`app.py:63` `_RECONCILE_INTERVAL`, `app.py:355`
calls `_reconcile_aiperf` after the per-tenant vLLM reconcile and the
diagnostics runner).

```
 AI Factory → Benchmark            daalu-api                  gpu-controller (every 30 s)
 ─────────────────────            ─────────                  ───────────────────────────
 "Run sweep"  ──POST /aiperf/runs──► AiperfRun(pending) ──►   _reconcile_aiperf
                                                               ├─ exec flag off → state=error, code=exec_disabled
                                                               └─ _run_one_aiperf
                                                                   ├─ build Job (aiperf + uploader sidecar)
                                                                   ├─ create on the endpoint's cluster, wait, read log
                                                                   ├─ parse stdout → summary.metrics
                                                                   ├─ list + parse uploaded artifacts → concurrency_levels
                                                                   └─ state = passed | failed | error
 panel polls GET /aiperf/runs ◄────────────────────────────── row updated
```

States (`models/aiperf_run.py:39`): `pending → running → passed | failed |
error`. `error` means the run could not execute (flag off, Job failed to
create or run); `failed` means it ran but nothing usable was parsed.

---

## 93.2 The exec gate: `gpu_aiperf_exec_enabled` (off by default)

`config.py:228` `gpu_aiperf_exec_enabled: bool = False`. When it is off, the
controller does not leave runs hanging: every pending row is finished
immediately as `error` with `code: exec_disabled` and a message telling the
operator why (`app.py:627-643`). `GET /ai-factory/aiperf/runs` also returns
`exec_enabled` (`gpu_metrics.py:729`) so the panel can say so before anyone
clicks.

**Why it is off.** A full concurrency sweep *is* load. Against a single shared
card it spikes latency for live tenants. The code comments
(`config.py:219-227`, `models/aiperf_run.py:17-18`) and the panel's hazard banner
(`aiperf-panel.tsx:81-92`) all make the same point: run it off-peak or against a
candidate node before traffic moves to it. The Job also bounds itself
(§93.4) so a click can't hammer a card for hours.

The flag is read by the **gpu-controller** process (it loads
`daalu-automation-secrets` via `envFrom`, `deploy/k8s/gpu-controller/deployment.yaml:34-39`).
Prod's SOPS file carries a `GPU_AIPERF_EXEC_ENABLED` key
(`deploy/k8s-prod/secrets.enc.yaml:44`); its value is encrypted, so check it
before you assume either state. Remember the gpu-controller is not
ArgoCD-managed (chapter 80 §80.6): after changing the secret, restart it by hand.

---

## 93.3 Who may run a sweep, and against what

Access is decided by `current_aiperf_access` (`gpu_metrics.py:606`):

| Caller | Allowed? | Target | Sees |
|---|---|---|---|
| Site superuser | yes | free choice: raw vLLM (`DEFAULT_TARGET_URL`, `aiperf.py:39`), the inference gateway (`_GATEWAY_URL`, `gpu_metrics.py:61`), or any explicit `target_url` | every run (`scope: "site"`) |
| Tenant admin who **owns** a GPU | yes | pinned to their own endpoint: the tenant's `sovereign_inference_url` if set, else `http://llm-classifier.<namespace>.svc.cluster.local:80` (`gpu_metrics.py:672-684`) | own runs only |
| Tenant admin who **provides** a shared pool | yes | pinned to their pool's `upstream_url`; model limited to `served_models` (`gpu_metrics.py:660-670`) | own runs only |
| Anyone else | 403 | — | — |

An owner/provider who passes `target_url` gets **403 "you may only benchmark
your own endpoint"** (`gpu_metrics.py:656-657`). Fetching another tenant's run
returns **404, not 403** (`_aiperf_run_or_404`, `gpu_metrics.py:734`) so run
ids don't leak.

**Why not superuser-only?** The model docstring (`models/aiperf_run.py:8-12`)
still describes the original superuser-only design. It was widened because the
plan's node-acceptance use case (§93.7) is something a GPU owner needs for
their own card, and the risk that justified superuser-only — loading the
*shared* stack — doesn't apply when the target is pinned to the caller's own
endpoint. Treat the docstring as stale; the router and `config.py:221-223` are
current.

The frontend repeats the gate (`benchmark/page.tsx` shows a
`FactoryGate` unless `is_superuser` or the overview role is `owner`/`provider`),
but the backend is the enforcement point.

---

## 93.4 The Job the controller builds

`aiperf_bench_job` (`aiperf.py:182`) returns a plain `batch/v1` Job dict.

**The AIPerf argv** (`aiperf_args`, `aiperf.py:68`) is `aiperf profile` with
`--model`, `--url`, `--endpoint-type` (default `chat`), `--streaming` (needed
to measure TTFT and ITL rather than only total latency), the concurrency sweep,
`--request-count`, `--synthetic-input-tokens-mean`, `--output-tokens-mean` and
`--artifact-dir=/artifacts`. The token flags are the **AIPerf 0.9.0** names;
the bare `--synthetic-input-tokens` / `--output-tokens` were removed upstream
(`aiperf.py:93-99`). If you pin an older image, these flags will not parse.

**Input bounds.** Applied twice — in the API (`gpu_metrics.py:703-706`) and in
the builder:

- concurrency: digits and commas only, deduplicated, each level capped at 1024,
  anything malformed falls back to `1,2,4,8,16,32` (`normalise_concurrency`,
  `aiperf.py:50`);
- requests per level ≤ 2000 (`_MAX_REQUEST_COUNT`, `aiperf.py:45`);
- input/output tokens ≤ 8192 (`_MAX_TOKENS`, `aiperf.py:46`).

Request defaults match `scripts/aiperf-bench.sh`: 200 requests, 512 input, 256
output tokens (`gpu_metrics.py:625-638`).

**Job spec decisions** (`aiperf.py:231-254`):

- **No GPU requested.** The aiperf container asks for 1–2 CPU and 1–2 GiB
  (`aiperf.py:124-128`). The GPU is the thing under test, on a different pod;
  requesting one would compete with it (and on a non-sliced card would never
  schedule).
- `backoffLimit: 0` — a load test is not idempotent, so a failure is reported,
  never retried.
- `activeDeadlineSeconds: 3600`, `ttlSecondsAfterFinished: 600`. The controller
  waits up to 3300 s (`app.py:703`) and deletes the Job itself afterwards.
- Name `daalu-aiperf-<run-id[:8]>`; `_run_job_capture` deletes any stale Job of
  the same name first (`app.py:814-820`), so re-runs are idempotent.

### The artifact-uploader sidecar

AIPerf writes structured results (`profile_export_aiperf.json/csv` and logs
per concurrency level) to `--artifact-dir`, which is an `emptyDir` and dies
with the pod. The controller therefore always adds a second container
(`_uploader_container`, `aiperf.py:145`), a MinIO client (`mc`) that:

1. waits for a `DONE` sentinel on a shared `/signal` volume (polling every 2 s,
   giving up after ~57 min, `aiperf.py:152-153`);
2. mirrors `/artifacts/` to `s3://<s3_bucket_aiperf>/aiperf/<run-id>/`,
   creating the bucket if needed; copy failures don't wedge the pod
   (`|| true`).

To write that sentinel, the aiperf container is wrapped in a tiny **`python3
-c` launcher** (`aiperf.py:134-141`) that runs `aiperf` as a subprocess,
writes `EXIT` and `DONE`, and exits with AIPerf's own code so a failed sweep
still fails the pod. Two non-obvious choices:

- **`python3`, not `/bin/sh`** — the AIPerf image is shell-less (it has
  `python3` and the `aiperf` console script only).
- **args passed as one JSON argv element** — sidesteps all shell quoting; the
  optional `Authorization: Bearer …` header contains spaces and colons.

Without an uploader (the builder's `artifacts_s3=None` path, used by tests and
hand runs) the image's entrypoint is called directly with bare `args`.

### Where the Job runs

`_aiperf_cluster_tunnel` (`app.py:733`) resolves the run tenant's `GpuTenant`
and uses its `target_cluster_tunnel_id`; no `GpuTenant` (or a null tunnel) means
the operator/hub cluster. The Job is created in namespace **`daalu`** on that
cluster (`app.py:679`, `:703`). The reasoning (`app.py:693-697`): AIPerf opens
connections to an in-cluster Service URL, so it must run where the Service,
the GPU and the object store actually are, not on the hub.

Two things to verify on a new cluster:

- **Credentials.** On a tunnelled cluster the controller uses the joined
  cluster's self-registered kubeconfig (chapter 80 §80.1), which is
  cluster-admin by default. On the **operator cluster** it uses its own
  ServiceAccount, whose ClusterRole (`deploy/k8s/gpu-controller/rbac.yaml`)
  grants namespaces, nodes, secrets, services, pods, deployments, PDBs and
  NetworkPolicies, but **no `batch/jobs` and no `pods/log`**. Job creation
  there will fail with a 403 that surfaces as `code: job_failed` until the
  role is extended.
- **Namespace.** For an owner whose stack lives in another namespace, the Job
  still runs in `daalu` and reaches the Service by its cluster DNS name; a
  NetworkPolicy that only admits same-namespace traffic would block it.

### What the runner does *not* pass

`_run_one_aiperf` never sets `auth_token` (`app.py:678-692`), so UI-started
runs send **no `Authorization` header**. Raw vLLM doesn't need one; a gateway
target that requires a bearer token (chapter 34) will reject the requests, and
the run will end `failed` with no metrics. Gateway-overhead comparisons with
auth are, today, a job for `scripts/aiperf-bench.sh --auth`.

---

## 93.5 Parsing: from stdout and from the artifact tree

Two independent parsers, both deliberately tolerant (they never raise):

1. **stdout** — `parse_aiperf_output` (`aiperf.py:289`) reads the pod log of
   the `aiperf` container (selected explicitly, since the pod has two
   containers, `app.py:701-704`). It first tries JSON, then scans the human
   table for six canonical metrics (`_METRIC_LABELS`, `aiperf.py:267`): TTFT,
   ITL, TPOT, request latency, output-token throughput, request throughput.
   From each row it takes the first number *after* the label (the average
   column), so a unit like `(ms)` inside the label is not mistaken for a
   value. `passed` means "at least one metric was extracted" — a benchmark has
   no pass/fail verdict. The last 4000 chars go into `summary.raw_tail`.
2. **artifacts** — `_collect_aiperf_artifacts` (`app.py:751`) lists
   `aiperf/<run-id>/` in the store, records a manifest (`summary.artifacts`,
   `summary.artifacts_bucket`), downloads only the per-level
   `profile_export_aiperf.json` files ≤ 2 MB (`app.py:780-789`), and hands them
   to `parse_aiperf_artifacts` (`aiperf.py:384`). That returns one row per
   concurrency level — `{concurrency, isl, osl, metrics, path}` — sorted by
   concurrency: the **SLO curve** the panel tabulates
   (`summary.concurrency_levels`). It understands the 0.9.0 layout
   (`concurrency_<n>/…`, ISL/OSL read from `input_sequence_length` /
   `output_sequence_length` in the report) and the legacy GenAI-Perf layout
   (`CON<n>` / `ISL<n>_OSL<n>` in the path), and skips the
   `*_aggregate.json` / `*_sweep.json` roll-ups.

A run whose stdout didn't parse but whose artifacts produced a curve is
promoted to `passed` (`app.py:720-724`). A storage error never fails the run;
it degrades to `summary.artifacts_error`.

**Not computed:** the model's comment on `summary` mentions "the saturation
knee" (`models/aiperf_run.py:73-74`), but no code computes one. Reading the
knee — where throughput flattens while TTFT/ITL climb — is left to the person
looking at the table.

---

## 93.6 Two object-store endpoints, one bucket

AIPerf artifacts are written **on the workload cluster** and read **from the
hub**, which sit on opposite sides of the WireGuard tunnel. Hence two endpoint
settings (`config.py:236-251`):

| Setting | Used by | Meaning |
|---|---|---|
| `gpu_aiperf_s3_endpoint` | the uploader sidecar (`app.py:671-677`) | the store as reached **inside** the workload cluster (e.g. its Ceph RGW) |
| `gpu_aiperf_s3_endpoint_hub` | the controller's readback and the API's download route (`object_storage.aiperf_client`, `object_storage.py:44`) | the **same** store reached from the hub, typically through a daalu-edge service proxy |
| `gpu_aiperf_s3_access_key` / `_secret_key` | both | shared credentials |
| `s3_bucket_aiperf` (default `daalu-aiperf`) | both | bucket; created on first upload |

Each falls back to the platform `s3_*` settings when empty, which is right for
a single-cluster dev install. If only the hub endpoint is missing, uploads
still succeed but the curve and downloads degrade to `artifacts_error`.

**Rejected alternative:** using the platform `s3_*` store for everything. The
writer runs on the workload cluster, which generally cannot reach the hub's
MinIO; the reports/events/attachments buckets also have nothing to do with
benchmark output.

### Downloads are bounded to the run's manifest

`GET /ai-factory/aiperf/runs/{id}/artifacts/{path}` (`gpu_metrics.py:772`)
serves a file **only if `path` is in that run's recorded manifest**, then
streams `aiperf/<run-id>/<path>` from the hub-side client. That one check
blocks path traversal and arbitrary-key reads in the bucket without any path
normalisation logic.

---

## 93.7 What the numbers are for (from the plan)

`04-aiperf.md` (internal design notes, not published) §2 lists four uses:
capacity planning (the concurrency at which a card saturates), the SLO and
tokens-per-second figures behind the pricing tiers, input for Dynamo's Planner,
and a **node-acceptance gate** (benchmark a new card before the router is
pointed at it). The code delivers the measurement and the curve. None of the
downstream consumers is wired: there is no automatic pricing back-fill, no
acceptance gate that blocks a router flip, and no CI performance-regression
check (`STATUS.md` (internal design notes, not published) §04, "pending").

For correlation with the device side, look at the DCGM utilisation, power and
memory panels in AI Factory for the same time window (chapter 80 §80.4–80.5):
AIPerf measures client-side latency, DCGM shows whether the card was the
bottleneck. vLLM's own `/metrics` (chapter 30, §Metrics) shows queue depth
and KV-cache use during the sweep.

---

## 93.8 Operational notes and what to verify

- **Image.** `AIPERF_IMAGE = harbor.daalu.io/ai-dynamo/aiperf:latest`
  (`aiperf.py:23`), uploader `minio/mc:latest` (`aiperf.py:27`). Both are
  meant to be mirrored and pinned (`gpu_aiperf_image`,
  `gpu_aiperf_uploader_image`) before real use; STATUS still lists that as
  pending. The image must be AIPerf ≥ 0.9.0 for the token flags.
- **A pod that never schedules** (image pull failure, quota) is not raised as
  an exception: `_run_job_capture` returns a "diagnostic pod never scheduled"
  string as the log (`app.py:837-841`), which parses to no metrics, so the run
  lands `failed` with that text in `raw_tail` rather than `error`.
- **Stdout and output** are truncated to the last 8000 chars in `run.output`
  (`app.py:716`).
- **First real use** is planned as the RTX 6000 / RTX 2000 acceptance run in
  the GPU-onboarding runbook (the internal design notes (not published)),
  which still names the script `scripts/bench-aiperf.sh`; the real name is
  `scripts/aiperf-bench.sh` (STATUS §14).
- **Hand runs.** `deploy/k8s/gpu/aiperf-bench-job.yaml` is outside the
  gpu-workloads ArgoCD include glob (`{01-,03-,05-,07-}*`,
  `deploy/argocd/applications/daalu-gpu.yaml:53-55`), so it never
  auto-applies. It has no uploader sidecar.

### Tests

`tests/test_aiperf.py` covers: concurrency normalisation, argv shape, the
no-GPU Job, the uploader-sidecar Job shape, the stdout parser (table, JSON,
empty), the artifact-curve parser (legacy and 0.9.0 layouts), superuser create
and list, owner/provider target pinning and the arbitrary-target 403, the
no-GPU-admin 403, tenant-scoped listing and 404, artifact list/download, and
`_aiperf_cluster_tunnel` resolution. Nothing exercises a real Job; the exec
path is cluster-only.

---

## Related

- [Chapter 30 — vLLM on the workload cluster](30-vllm-deployment.md): the
  default target.
- [Chapter 34 — The inference-gateway service](34-inference-gateway.md): the
  "front door" target.
- [Chapter 80 — Zero-touch GPU onboarding](80-zero-touch-gpu-onboarding.md):
  the gpu-controller, tunnels and per-tenant Prometheus.
- [Chapter 94 — GPU sharing](94-gpu-sharing-time-slicing-and-mps.md): why a
  sweep on a shared card affects neighbours.
- [Chapter 95 — GPU health](95-gpu-health-dcgmi-diag-and-nvsentinel.md): the
  sibling `dcgmi diag` runner built on the same queue-and-Job pattern.
