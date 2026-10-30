# 73. Coding-model catalog & multi-model serving

Chapters 35/36 cover the coding-assistant plumbing and the operator
runbook. This chapter is the nuts-and-bolts of **how a user gets to pick
a model** and how that pick flows all the way to the right vLLM. If
you're adding a model, debugging a "model not found", or wiring the
RTX 6000 Ada, start here.

---

## The single source of truth: `core/model_catalog.py`

Every selectable coding model is one frozen `CodingModel` dataclass entry:

```python
CodingModel(
    id="qwen2.5-coder-14b",                      # stable key (DB, API, config)
    label="Qwen2.5-Coder 14B",                   # UI text
    served_model_name="qwen/qwen2.5-coder-14b-instruct",  # the contract string
    gpu_class="ada-16",                          # ada-16 (16GB) | ada-48 (48GB)
    hf_model="Qwen/Qwen2.5-Coder-14B-Instruct-AWQ",
    quantization="awq_marlin",                   # "" lets vLLM infer (FP8)
    vram_gb=16,
    context_len=8192,                            # --max-model-len
    description="…",
    default=True,
    tool_call_parser="hermes",                   # → --enable-auto-tool-choice
)
```

The catalog now carries three entries (`core/model_catalog.py:63-120`):
`qwen2.5-coder-14b` (`ada-16`, the catalog-flag default), `qwen2.5-coder-32b`
(`ada-48`, dense) and **`qwen3-coder-30b-a3b`** (`ada-48`, MoE 30B total / 3B
active, FP8, 65536 context) — which is the **settings default**:
`config.py:284-287` sets both `coding_models_enabled` and
`coding_model_default` to `qwen3-coder-30b-a3b`, and it fits a single 48 GB
card (e.g. RTX 6000 Ada). (The catalog's `default=True` flag and the settings default are
independent; the *settings* value wins for a tenant — the flag only orders the
UI.)

`tool_call_parser` is the field that made the agentic loop work natively: when
set, the rendered Deployment gets `--enable-auto-tool-choice
--tool-call-parser=<value>` so the model emits OpenAI function calls instead of
us regex-parsing text pragmas. Qwen2.5 uses the `hermes` parser; Qwen3-Coder
ships its own `qwen3_coder` parser — launching it without these flags produced
the `"auto" tool choice requires --enable-auto-tool-choice` 400.

The module is **pure data** — it imports nothing else in the package, so
daalu-api, the workspace-controller, and the gpu-controller can all
import it without a circular dependency. It sits in `core/` deliberately
(the shared-primitives layer), not in `config.py` (settings only) or
`models/` (ORM only). It mirrors the `api/routers/onboarding.py:_CATALOG`
pattern, promoted to `core/` for cross-service reuse.

Helpers: `all_models()`, `get(id)`, `default_model()`,
`is_servable(id, enabled_ids)`, `served_name(id)` (resolves to a valid
served name even for a stale/unknown id).

---

## The five-names contract

For any model, **one string** must be identical in five places, or
inference silently returns a 404 from vLLM:

```
catalog.served_model_name
  ── vLLM --served-model-name        (gpu_controller/manifests.py)
  ── DAALU_MODEL env in the pod      (workspace_controller/k8s.py)
  ── the `model` field the pod sends (chat_repl.py / the extension)
  ── GpuPool.served_models[]         (multi-pool routing, later)
```

Three of those four are *derived* from the catalog entry at runtime:
`manifests.coder_params_from_catalog(entry)` fills the vLLM flag,
`k8s._container_env` resolves `DAALU_MODEL` via `model_catalog.served_name`,
and the CLI/extension just echo `DAALU_MODEL`. Keep deriving them — never
hand-type the served name in a second place.

`vLLM --served-model-name` decouples the served id from the HF repo, so
you can swap `Qwen/...-AWQ` for an FP8 build without touching anything
downstream.

---

## Request path: pick → persist → inject → route

```
Browser CreateWorkspaceForm
  │  GET /ide/models  → catalog + `available` (from coding_models_enabled)
  │  POST /ide/sessions {model: "qwen2.5-coder-14b", …}
  ▼
daalu-api  api/routers/workspace.py
  │  resolve model or coding_model_default
  │  is_servable()? no → 422
  │  forward to controller (model in body)
  ▼
workspace-controller  app.py → k8s.py
  │  persist workspaces.model
  │  inject DAALU_MODEL = served_name(model) into the code-server container
  ▼
workspace pod  (daalu chat / Daalu Assistant extension)
  │  POST {DAALU_GATEWAY_URL}/v1/chat/completions {model: DAALU_MODEL}
  ▼
inference-gateway  inference_gateway/pool.py:resolve_upstream(model)
  │  pool whose served_models contains model → its upstream
  │  else catch-all pool (empty served_models)
  │  else settings.daalu_hosted_upstream_url  (single-model bootstrap)
  ▼
vLLM (qwen-coder / llm-coder) on the GPU cluster, over the WG tunnel
```

Validation is two-layered: the catalog's `available` flag *displays*
what's servable; `create_session`'s 422 is the *authoritative* server-side
reject (never trust the client). A 422 means "valid request, but that
model isn't deployed" — distinct from the 403 the eligibility gate raises.

---

## Manifest parameterization

`gpu_controller/manifests.py` was single-deployment (`llama-classifier` /
`llm-classifier` hardcoded). It now parameterizes `deployment_name`,
`service_name`, `network_policy_name`, and `mem_request`/`mem_limit` on
`GpuParams`, so a second, co-resident stack doesn't collide. The coder is
named `qwen-coder` (Deployment) / `llm-coder` (Service).

* `build_all(params, include_shared=False)` omits the Namespace, the
  `hf-token` Secret, and the NetworkPolicy (namespace singletons) — use it
  for the *second* stack in a namespace the first already provisioned.
* `coder_params_from_catalog(entry)` builds `GpuParams` from a catalog
  entry so the served name/HF repo/quant/context all come from one place.
* `daalu render-coding-model <id> [--gpu-class]` prints the bundle:
  `daalu render-coding-model qwen2.5-coder-14b | kubectl apply -f -`.

---

## GPU sizing & the exclusivity constraint

The NVIDIA device plugin allocates a physical GPU **exclusively** to one
pod (`resources.limits.nvidia.com/gpu: 1`). So:

| Card | `gpu-class` | Fits |
|---|---|---|
| RTX 2000 Ada (16 GB) | `ada-16` | one ~14B AWQ-INT4 coder (sole vLLM on the card) |
| RTX 6000 Ada (48 GB) | `ada-48` | a 32B dense (AWQ) **or** the 30B-A3B MoE (FP8) |

You cannot run the 8B classifier *and* a 14B coder on one 16 GB card —
the second pod stays `Pending` (no allocatable GPU), and even time-sliced
the VRAM won't hold both.

**Recommended split on a 48 GB node:** the classifier serves on the GPU
cluster's tunnel `:8001` and the coder (`qwen-coder` Deployment / `llm-coder` Service, running
`qwen3-coder-30b-a3b`) serves on `:8000` — port convention **8000 = coder,
8001 = classifier**, one edge socat proxy per port. daalu-api reaches the coder
via e.g. `CODING_LOCAL_BASE_URL=http://10.200.0.2:8000/v1` (a dedicated
setting so the coder upstream is wired independently of `llm_local_base_url`,
which points at the classifier and 404s coding models; see
[77](77-daalu-cli.md)). Whether a coder is *actually* serving is probed at
workspace-eligibility time (`core/coding_gateway.py:coding_upstream_healthy`) rather than assumed from configuration.

---

## Routing: today's bootstrap vs the multi-pool seam

**Today (one model):** no `gpu_pools` rows. `resolve_upstream` falls back
to `settings.daalu_hosted_upstream_url` → the single coder vLLM. The pod's
`model` field must still equal the served name (vLLM 404s on a mismatch),
which the catalog guarantees.

**When `ada-48` lands (multiple models):** register a `GpuPool` per model
with an explicit `served_models=[served_name]` and the model's upstream
URL. `resolve_upstream` now (chapter 34 + this change) **prefers an exact
`served_models` match** over an empty-list catch-all, breaks ties by
`created_at`, and only falls back to the env upstream when nothing
matches. This stops a `served_models=[]` catch-all (e.g. a legacy pool)
from silently swallowing coder requests and 404ing them against the wrong
vLLM. Give *every* pool an explicit `served_models` list before
introducing a second model.

---

## Adding a model — checklist

1. Add a `CodingModel` entry to `core/model_catalog.py` (pick the right
   `gpu_class`, `hf_model`, `quantization`).
2. `daalu render-coding-model <id> --gpu-class <ada-NN>` → apply to the
   GPU cluster (free the card first if it's `ada-16`).
3. Confirm `curl <svc>/v1/models` advertises the served name.
4. Point routing at it: either set `DAALU_HOSTED_UPSTREAM_URL` (single
   model) or register a `GpuPool` with `served_models=[served_name]`
   (multi-model).
5. Add the catalog id to `CODING_MODELS_ENABLED` so it's selectable.
6. (No migration needed — `workspaces.model` is a free-form catalog id.)

---

## See also

* Chapter 34 — inference-gateway + `resolve_upstream`.
* Chapter 35 — coding-assistant architecture.
* Chapter 36 — operator runbook (deploying the coder, the 502 fix).
* Chapter 30 — vLLM deployment fundamentals.
* Customer book chapter 26 — the model-picker UX.
