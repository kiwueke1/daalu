# 94. GPU sharing — time-slicing, MPS, and the per-tenant memory fraction

Chapter 32 introduced time-slicing in a paragraph, as a dev/test trick. Since
then it has become how Daalu runs **two vLLM servers on one 48 GB card** (the
SRE model plus the lab workload the S1-01 demo breaks), and the repo has grown
an MPS profile for the day the card is shared between tenants. This chapter
covers what is in the tree for both, what is actually applied, and the knob
that makes time-slicing safe enough to use: the per-tenant
`--gpu-memory-utilization` stored on `gpu_tenants`.

"Sharing" means two different things in this codebase. Keep them apart:

| Layer | Question it answers | Where |
|---|---|---|
| **Device sharing** (this chapter, §94.1–§94.4) | how many pods can request `nvidia.com/gpu` on one physical card, and what stops them hurting each other | device-plugin ConfigMaps in `deploy/k8s/gpu/`, `gpu_controller/manifests.py` |
| **Tenant sharing** (§94.5) | may tenant B send inference to tenant A's card, and who gets billed | `core/gpu/sharing.py`, `models/gpu_pool.py`, `inference_gateway/pool.py` |

| Piece | Path |
|---|---|
| Time-slicing profiles | `deploy/k8s/gpu/01-time-slicing.yaml` (ConfigMap `time-slicing-config`) |
| MPS profiles | `deploy/k8s/gpu/07-mps-config.yaml` (ConfigMap `mps-config`) |
| How a node gets a profile | `scripts/deploy-gpu.sh:435-467` (step 3) |
| Per-tenant memory fraction | `models/gpu_tenant.py:111-118`, migration `0033_gpu_tenant_mem_util.py`, `gpu_controller/manifests.py:377` `classifier_params` |
| Catalog defaults | `core/model_catalog.py:186-189`, `:215` |
| Tenant-sharing guard + pool | `core/gpu/sharing.py`, `gpu_controller/app.py:145-152`, migration `0022_gpu_sharing.py` |
| Tests | `tests/test_gpu_sharing.py`, `tests/test_gpu_controller_manifests.py` |
| Plans | `01-node-enablement.md` (internal design notes, not published) §3.2, §4.3, §5; `13-gpu-sharing-and-multi-tenant-marketplace.md` (internal design notes, not published) |

---

## 94.1 The three levers, and why MIG isn't one of them

The NVIDIA k8s-device-plugin can advertise one physical GPU as several
`nvidia.com/gpu` resources. What each lever isolates
(`01-node-enablement.md` (internal design notes, not published) §5):

| Lever | Compute | VRAM | Fault domain | On our cards |
|---|---|---|---|---|
| **Time-slicing** | time-multiplexed | **shared, no limit** | shared | yes — applied on gpu-node-1 |
| **MPS** (Multi-Process Service) | per-client active-thread % | **per-client limit** | shared CUDA context | supported by the cards; profile in repo, not applied |
| **MIG** | hardware partition | hardware partition | isolated | **no** — the RTX 2000 Ada and RTX 6000 Ada don't support MIG |

**Rejected: MIG.** It is the only real hardware isolation, but it exists only on
datacentre Ampere/Hopper/Blackwell parts. The plan says explicitly not to
install the MIG Manager (it would sit idle); MIG stays the future path for a
tenant who brings MIG-capable hardware (§4.2 of the plan).

**Rejected (for now): DRA.** Dynamic Resource Allocation was researched and not
adopted; see the internal design notes (not published).

So multi-tenant isolation on Daalu hardware is **soft** by construction:
time-slicing or MPS, plus gateway quotas and rate limits.

---

## 94.2 Time-slicing: `01-time-slicing.yaml`

One ConfigMap, `time-slicing-config` in namespace `gpu-operator`, with a
profile per `gpu-class` (`01-time-slicing.yaml:15-41`):

| Key | `replicas` | Card |
|---|---|---|
| `ada-16` | 4 | RTX 2000 Ada 16 GB |
| `ada-48` | 8 | RTX 6000 Ada 48 GB |
| `blackwell-32` | 6 | RTX 5090 32 GB (unused) |

Every profile sets:

- `renameByDefault: false` — the resource stays `nvidia.com/gpu`, so no
  workload manifest changes its request;
- `failRequestsGreaterThanOne: true` — a pod asking for 2 "GPUs" is refused,
  because two slices of one card are not two cards.

**How a node gets a profile.** The device plugin reads one ConfigMap, named in
the ClusterPolicy (`spec.devicePlugin.config.name`), and picks the key named
by the node label `nvidia.com/device-plugin.config`. `scripts/deploy-gpu.sh`
step 3 does all three: applies the ConfigMap, patches the ClusterPolicy to
`{"name":"time-slicing-config","default":"<gpu-class>"}`, and labels the node
(`deploy-gpu.sh:449-462`). With `--skip-operator` (the standalone
device-plugin path from chapter 32), step 3 is skipped and the plugin's own
ConfigMap is used instead.

**What's applied.** On gpu-node-1 (RTX 6000 Ada) the `ada-48` profile is live and
the node advertises `nvidia.com/gpu: 8`
(`docs/content/shorts/lab-llm-serving/setup-log-2026-09-27.md`, row 4).

**What time-slicing does not do.** The 8 is a scheduling fiction: all eight
pods see the whole 48 GB, and nothing stops one from allocating all of it.
Which leads to the knob in §94.3.

---

## 94.3 With time-slicing, the memory budget is yours: `gpu_memory_utilization`

vLLM reserves `--gpu-memory-utilization` × total VRAM at startup (weights plus
KV cache) and refuses to start if that much isn't free. On a time-sliced card,
the sum of every vLLM's fraction must stay under 1.0, and the **start order**
matters: whoever starts first gets its share.

### Why it had to move into the database (PR #161, migration 0033)

The gpu-controller re-renders and re-applies the tenant's classifier
Deployment on **every 30 s tick** (`gpu_controller/app.py:411`
`_do_apply`). It used to emit a hard-coded `--gpu-memory-utilization=0.90`, so
a card could never hold a second vLLM: a `kubectl edit` was reverted within a
tick (migration docstring, `migrations/versions/0033_gpu_tenant_mem_util.py:1-12`).

Now the fraction is resolved in this order (`manifests.py:377-406`
`classifier_params`):

1. `GpuTenant.gpu_memory_utilization` if non-NULL
   (`models/gpu_tenant.py:111-118`, passed in at `app.py:414`);
2. else the model's catalog value (`core/model_catalog.py:189`, default 0.90);
3. else `GpuParams.gpu_memory_utilization = 0.90` (`manifests.py:55`) for a
   model not in the catalog.

It ends up as a vLLM arg at `manifests.py:123`. The pod still requests exactly
`nvidia.com/gpu: 1` (`manifests.py:179`, `:183`) — one slice.

**Design choice: NULL means "catalog default".** The column is additive and
nullable, so every existing row kept rendering exactly what it already ran.

**Defence in depth in the catalog.** The `qwen/qwen3.6-35b-a3b` entry ships
with **0.85**, not 0.90 (`core/model_catalog.py:211-215`): if a reconnect
creates a fresh `gpu_tenants` row without the override, the SRE model must
still fit beside the lab's 0.10 workload, or vLLM refuses to start.

### How to set it

There is no API or UI field for it. `GpuSpec` (the controller's upsert body)
doesn't carry it, so it is set on the row directly, as the lab did:

```sql
UPDATE gpu_tenants
   SET model_classifier = 'qwen/qwen3.6-35b-a3b', gpu_memory_utilization = 0.85
 WHERE tenant_id = '<tenant>';
```

The next tick re-renders the Deployment. `GET /onboarding/gpu/serving`
(`api/routers/onboarding.py:1743`) reports the fraction the controller
*renders* (it calls the same `classifier_params`, `onboarding.py:1763-1767`),
so the GPU page shows what's actually on the card, not a form value.

### The gpu-node-1 budget (from the lab)

From `docs/content/shorts/lab-llm-serving/README.md` §"GPU budget":

| Server | Fraction | ≈ VRAM |
|---|---|---|
| SRE model (Qwen3.6-35B-A3B-FP8, FP8 KV, 32k context) | 0.85 | ~40.8 GiB |
| `chat-llm` (1.5B, the lab workload) | 0.10 | ~4.8 GiB |
| total | 0.95 | fits in either start order |

Raising the SRE model past 0.88 leaves no room for `chat-llm` at 0.10; the lab
notes say cut `--max-model-len` instead. Setting `chat-llm` to 0.90 is the
S1-01 break: vLLM exits at startup asking for ~43 GiB with ~7 GiB free.

Related alert: `GpuMemoryNearFull` (`deploy/k8s/monitoring/04-alerting-rules.yaml:181-198`)
fires at > 97 % framebuffer for 10 min. Its description calls out an
over-eager `--gpu-memory-utilization` and "too many MPS clients" as causes.

---

## 94.4 MPS: `07-mps-config.yaml` (in the repo, not applied)

MPS runs all clients inside one CUDA context and lets the device plugin
**enforce** a per-client VRAM limit (`CUDA_MPS_PINNED_DEVICE_MEM_LIMIT`) and
active-thread percentage (`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` ≈ 100/replicas),
both derived from `replicas` (`07-mps-config.yaml:44-49`). A noisy client can no
longer OOM its neighbour. It is **not** a fault boundary: a client that faults
can still wedge the shared context (`07-mps-config.yaml:8-10`).

The file is a ConfigMap `mps-config` in `gpu-operator` with two profiles
(`07-mps-config.yaml:36-61`):

| Key | `replicas` | ≈ VRAM per client |
|---|---|---|
| `ada-48-mps` | 8 | ~6 GB |
| `ada-48-mps-4` | 4 | ~12 GB |

Rules stated in the file:

- **Time-slicing and MPS are mutually exclusive per card.** Keep `ada-16` on
  time-slicing; point a *shared* RTX 6000 Ada at an MPS profile.
- MPS also needs the GPU Operator's MPS control daemon enabled in the
  ClusterPolicy; that toggle is not in this file.

**Why MPS for the multi-tenant case.** The plan
(`01-node-enablement.md` (internal design notes, not published)
§4.3, §5; `13-…` (internal design notes, not published)
§4) picks MPS plus gateway quotas as the production isolation answer for the
48 GB Ada because MIG is unavailable and time-slicing gives one tenant's KV
cache no protection from another's. Time-slicing remains right for Daalu's own
trusted workloads, where the operator sets every fraction.

### Status and a gap to verify before applying it

- **Not applied** to any cluster
  (`STATUS.md` (internal design notes, not published) §01: "not applied to a
  cluster … MPS needs the GPU Operator `sharing.mps` value set cluster-side").
  The file *is* inside the `daalu-gpu` ArgoCD include glob
  (`{01-,03-,05-,07-}*`, `deploy/argocd/applications/daalu-gpu.yaml:53-55`), so
  wherever that app syncs, the ConfigMap exists, but nothing points a device
  plugin at it.
- **The two ConfigMaps don't compose.** The comment at
  `07-mps-config.yaml:15-17` says `scripts/deploy-gpu.sh` step 3 handles the
  ClusterPolicy patch for MPS too. It doesn't: step 3 always points the
  ClusterPolicy at `time-slicing-config` (`deploy-gpu.sh:459-460`), and
  labelling a node `ada-48-mps` names a key that ConfigMap doesn't contain.
  Because the device plugin reads one ConfigMap, either point the ClusterPolicy
  at `mps-config` (dropping the time-slicing keys for that cluster) or merge the
  MPS keys into `time-slicing-config`, which is what the plan originally
  proposed (`01-node-enablement.md` §5.1). Decide that, and enable the MPS
  daemon, before the first apply.
- No code reads MPS state. The gpu-controller doesn't know whether a card is
  time-sliced or MPS-shared; it renders the same Deployment either way, and
  under MPS the vLLM fraction must also fit inside the per-client limit.

---

## 94.5 Tenant sharing: the provider guard and the shared pool

The other meaning of "sharing" (plan doc 13): a granted **GPU provider**
offers its card, through the inference gateway, to **other** tenants who pay
per token. A tenant's private (SOVEREIGN) GPU is never shared: its
`sovereign_inference_url` is read only from that tenant's own row (plan 13 §0).

What the code has:

- **`Tenant.is_gpu_provider`** (`models/tenant.py:36`) and
  **`GpuTenant.shared`** (`models/gpu_tenant.py:131`). Only a provider may set
  `shared`, enforced three times:
  1. the gpu-controller upsert returns **403** (`gpu_controller/app.py:145-152`);
  2. `core/gpu/sharing.py:46` `register_pool` raises `SharingError`;
  3. a Postgres **trigger** `gpu_tenants_shared_requires_provider`
     (`migrations/versions/0022_gpu_sharing.py:36-55`). A trigger, because a
     CHECK constraint can't query `tenants` (rejected alternative noted in the
     migration docstring).
- **`GpuPool`** (`models/gpu_pool.py:30`): the registry the gateway resolves
  upstreams from (`inference_gateway/pool.py:41` `resolve_upstream`; falls back
  to `settings.daalu_hosted_upstream_url` when no pool row exists).
- **Revenue share**: `GpuRevenueShare` (`models/gpu_revenue_share.py:31`),
  provider credit = gross × (1 − `platform_take_rate`) (`config.py:194`,
  default 0.0).
- AIPerf's provider scope (chapter 93 §93.3) benchmarks the pool's
  `upstream_url`.

**Not built** (STATUS §13): the HTTP endpoints for `grant_provider` /
`provider_earnings`, the onboarding route that calls `register_pool` when a
provider's stack goes `active` + `shared` (the service functions exist with
no callers), surfacing `shared` in the onboarding UI, and the prod migration.
In practice no tenant shares a card with another tenant today; the only
co-tenancy on gpu-node-1 is two workloads of the *same* tenant under time-slicing.

### Tests

`tests/test_gpu_sharing.py`: provider credit / take rate, engine selection in
the manifest, `register_pool` refusing a non-provider and an unshared stack,
gateway upstream selection and fallback, model-specific pool matching, and
revenue-share aggregation. No test touches the device-plugin ConfigMaps; they
are plain YAML applied by hand or by ArgoCD.

---

## 94.6 What to verify on camera or in a review

- `kubectl get node -o custom-columns=NAME:.metadata.name,GPU:.status.capacity.nvidia\\.com/gpu`
  shows the replica count, not the card count.
- `kubectl get clusterpolicy cluster-policy -o jsonpath='{.spec.devicePlugin.config}'`
  names the ConfigMap actually in effect; the node label names the key.
- The classifier Deployment's args show the fraction from the row, and a hand
  edit reverts within 30 s.
- The sum of every vLLM fraction on the card stays below 1.0 with room for the
  CUDA context overhead; the lab keeps it at 0.95.
- Never describe MPS as isolation: it enforces limits inside a shared fault
  domain.

---

## Related

- [Chapter 32 — NVIDIA-specific notes](32-nvidia-notes.md) (§Time slicing, the
  standalone device-plugin path, GSP hangs).
- [Chapter 30 — vLLM on the workload cluster](30-vllm-deployment.md).
- [Chapter 80 — Zero-touch GPU onboarding](80-zero-touch-gpu-onboarding.md):
  the gpu-controller reconcile loop and `gpu-class` derivation.
- [Chapter 93 — AIPerf load testing](93-aiperf-load-testing.md): measuring
  what a shared card can actually serve.
- [Chapter 95 — GPU health](95-gpu-health-dcgmi-diag-and-nvsentinel.md):
  `dcgmi diag` requests one slice, so on an unshared card it waits for a free
  GPU.
