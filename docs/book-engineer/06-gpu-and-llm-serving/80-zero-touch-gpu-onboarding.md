# 80. Zero-touch GPU onboarding & the AI-factory data plane

Chapters 30–34 cover the serving stack (vLLM, the router, the gateway). This
chapter covers **how a GPU gets from "node joined the customer cluster" to
"serving and visible in the UI" with no manual steps** — the PR #107/#108
zero-touch work — plus the deploy-progress surface (#109), the tenant-scoped
metrics data plane (#110), and the multi-GPU scrape topology that makes a
second GPU node visible at all.

| Piece | Path |
|---|---|
| Onboarding routes | `api/routers/onboarding.py` (`/gpu/nodes`, `/gpu/provision`, `/gpu/progress`, from line 1445) |
| GPU controller | `src/daalu_automation/gpu_controller/` (`app.py`, `k8s.py`, `manifests.py`) — **not** ArgoCD-managed |
| Shared health probe | `core/gpu/health.py:probe_openai_endpoint` |
| Port constants | `core/wireguard.py:57-58` (`VLLM_PROXY_PORT=8001` classifier, `CODER_PROXY_PORT=8000` coder) |
| Tenant Prometheus resolution | `core/prometheus.py:client_for_tenant` (line 161) |
| Multi-GPU DCGM scrape | `deploy/k8s/gpu/14-dcgm-nodeport.yaml` + `deploy/k8s/monitoring/07-gpu-tunnel-dcgm.yaml` |

---

## 80.1 The three manual preflight steps that were removed (PR #107)

Adding a GPU from the UI used to require three *undocumented* manual steps,
each failing late and unhelpfully when skipped:

1. **`kubectl label node <n> gpu-class=…`.** The vLLM Deployment selects on
   `gpu-class`, a label no stock GPU-operator install creates. Without it the
   pod sat Pending until the provision poll expired, with nothing in the UI
   explaining why. Now the class is **derived from GPU Feature Discovery's
   `nvidia.com/gpu.memory` label** (`gpu_controller/k8s.py:29-54` — VRAM MiB →
   `ada-16`/`ada-48`) and stamped on the node during apply
   (`gpu_controller/app.py:_ensure_node_labelled`, line 430: labels the
   pinned `row.gpu_node`, or every GPU node whose derived class matches — so
   a single-GPU cluster needs no node selection at all). `/onboarding/gpu/nodes`
   also returns `gpu_product`, so the form shows the discovered card.
2. **Hand-minting a cluster credential.** The kubeconfig is now
   **self-registered by the edge** at bootstrap (chart ServiceAccount +
   long-lived token Secret, reported in the callback; the hub assembles the
   kubeconfig and upserts the cluster's `kubernetes` Integration — chapter
   25). A rebuilt cluster replaces its own stale credential instead of
   surfacing a bare 401 weeks later.
3. **A follow-up `helm upgrade` for serviceProxies.** The edge chart defaults
   `serviceProxies: []`, so nothing listened on the inference ports. The
   generated `helm install` snippet now pre-wires **both** proxies
   (`core/wireguard.py:399-423`): `:8001 → llm-classifier`,
   `:8000 → llm-coder`.

Also fixed: a latent port bug where `onboarding._VLLM_PROXY_PORT` said 8000 —
the *coder* port — so the form suggested the coding stack's endpoint while the
served-model-name said llama. Both ports now come from **one constant pair in
`core/wireguard.py`**, shared by the snippet and the form so they cannot
drift. The convention is fixed: **8000 = coder, 8001 = classifier.**

### Provisioning is async (202)

`POST /onboarding/gpu/provision` (`onboarding.py:1645`) used to block up to
900 s waiting for vLLM; the edge proxy in front of the API cuts idle
connections at ~100 s, so **every successful deploy surfaced in the browser
as `Failed to fetch`** while the deploy quietly continued server-side. It now
returns **202** once the gpu-controller accepts the spec, and the client
polls `GET /onboarding/gpu` (which it already did). Registering the SOVEREIGN
router config up front is safe because the router's cascade falls through to
the next tier on transport failure — a still-booting endpoint behaves like a
momentarily-down GPU. The NV-CM provision flow later adopted the identical
pattern (PR #114, chapter 23 §23.2).

---

## 80.2 Eligibility must probe, not assume (PR #108)

Workspace eligibility used to key on `sovereign_inference_url` — which the
**classifier** sets. Workspaces consume the **coder**, a separate vLLM stack
(`qwen-coder`/`llm-coder`) that nothing in the UI deploys and that cannot
even co-exist with the classifier on a single-GPU node. So provisioning a GPU
switched workspaces on, the workspace created fine, the IDE opened — and the
first prompt failed against an upstream that was never deployed
(`resolve_coding_upstream` is static config resolution, not health-aware, so
nothing softened it).

`/workspace/eligibility` and `/workspace/models` now probe the resolved
upstream's `/v1/models` (`core/coding_gateway.py:coding_upstream_healthy` —
30 s TTL, **failures cached too**: an undeployed coder is the steady state
being detected). No coder serving → **409** naming the actual problem; every
model in the picker reports `available: false`. The
`feature_flags.workspaces_enabled` override bypasses the probe so an operator
can set up ahead of deploying the coder. The probe hits the models *listing*
so a form render never bills an inference. (More in chapters 73/77.)

The GPU *card* had the sibling bug (PR #109): `_gpu_status` never set
`healthy`, so it reported the schema's `False` default — the card said
"Offline" while `/billing/local-gpu`'s real probe said "online" on the same
page. Both now share one cached probe, `core/gpu/health.py:probe_openai_endpoint`
(only `200` counts — unlike the coding-upstream probe, where a gateway 401
still proves routing), so they cannot drift.

---

## 80.3 Deploy progress: making the three silent waits visible (PR #109)

A first vLLM boot is three sequential multi-minute waits — pull a ~10 GB
image, pull ~5 GB of weights, compile CUDA graphs — that look identical from
outside, and before the container starts there are **no logs at all**.
`GET /onboarding/gpu/progress` (`onboarding.py:1630` → gpu-controller
`/tenants/{id}/progress`) returns pod phase, per-container reason
(`ImagePullBackOff`, `CrashLoopBackOff`, …), namespace events, and vLLM logs
once they exist. The billing card renders a collapsible "Deploy progress"
panel that polls **only while open** — each poll reaches through the tunnel
to the customer's apiserver.

Example: the panel surfaces a vLLM pod
crash-looping on `createContainer hook: /usr/bin/nvidia-ctk: no such file`,
which from the outside was indistinguishable from a healthy slow boot. (That
error is the k3s GPU-operator-toolkit conflict — the operator's toolkit must
be **disabled** on k3s; see chapter 32.)

Related honesty fix: AI Factory showed a green "Healthy" pill above "No GPUs
are reporting metrics yet" because `worstOf([])` is vacuously "ok". The pill
is now gated on having ≥1 GPU, and the empty state explains that the page
reads **DCGM series from Prometheus** — independent of whether a GPU is
deployed and serving.

---

## 80.4 Query the tenant's Prometheus, not only the hub's (PR #110)

GPU metrics come from a dcgm-exporter running **in whichever cluster the card
is in**. When that's a customer cluster over the tunnel, its series live in
*that* cluster's Prometheus — but AI Factory built a bare
`PrometheusClient()` against the hub's `prometheus_base_url`, which has none
of them: a GPU that was demonstrably serving rendered "No GPUs are reporting
metrics yet".

`PrometheusClient` now takes a `proxy`, and
`core/prometheus.py:client_for_tenant` (line 161) resolves in order:

1. the tenant's `prometheus` Integration bound to a **connected** tunnel →
   dialed through the edge forward proxy, so `*.svc.cluster.local` URLs
   resolve inside the customer cluster;
2. the same Integration with no tunnel (directly reachable URL);
3. `settings.prometheus_base_url` — the hub's own store (keeps single-cluster
   installs working with no integration row at all).

A tunnel-bound row whose tunnel is *down* explicitly falls back to the hub
store rather than resolving an in-cluster URL against the hub and silently
returning nothing. `/ai-factory/overview` resolves the same client, so
`metrics_available` can no longer describe an endpoint none of the metric
routes read.

**The multi-tenant cache leak this exposed:** `cached_query` keyed on PromQL
alone — safe while every query hit one hub Prometheus, but the moment queries
can target different stores it hands one tenant's series to another for the
TTL. Now keyed on `(proxy, base_url, promql)`. Remember this pattern any time
a per-tenant backend gets introduced behind a previously-global cache.

Two cluster-side prerequisites: the GPU operator ships its dcgm **ServiceMonitor
disabled**, and raw DCGM series carry **no `tenant` label**, so they match
nothing under `tenant_selector` — which is the *security boundary*, not a
filter. And the Service-name trap: one kube-prometheus-stack release may name it
`kube-prometheus-stack-prometheus` while another (e.g. the hub's) uses
`kube-prom-stack-kube-prome-prometheus` — pasting the hub's name gives a
green connection Test and zero data.

---

## 80.5 Multi-GPU scrape topology

A second GPU node can be invisible on the AI-factory page after onboarding.
Two independent causes, both structural:

- **Data plane.** The hub Prometheus scraped a single socat tunnel port
  pointed at the dcgm-exporter **ClusterIP**, which load-balances to one pod
  per scrape — so only one node ever reported. The fix is a **node-pinned
  NodePort** (`externalTrafficPolicy: Local`,
  `deploy/k8s/gpu/14-dcgm-nodeport.yaml`), **one edge socat proxy per GPU
  node** (`:9400 → gpu-node-1`, `:9401 → gpu-node-2`), and a hub ServiceMonitor scraping
  both ports (`deploy/k8s/monitoring/07-gpu-tunnel-dcgm.yaml`). Adding a GPU
  node means adding a port to this chain — `scripts/deploy-gpu.sh` documents
  the pattern.
- **Backend.** `gpu_summary` keyed cards by the DCGM `gpu` index — and every
  single-GPU node reports `gpu="0"`, so two cards collapsed into one row.
  Cards are keyed by the unique GPU **UUID** (`id`), and the per-card
  timeseries/events/alerts are scoped by `?card=<uuid>` instead of the
  ambiguous index.

Related self-check fix: the observability self-check hard-coded
`up{job="nvidia-dcgm"}`, but DCGM series are federated over the tunnel
under per-tenant jobs (`gpu-tunnel-dcgm-<tenant>`), so `dcgm_target_up`
reported a false FAIL while every other check passed. It now asks the
question that matters — "is a target that emits DCGM GPU-temp series
currently up?" — by intersecting `up` with `DCGM_FI_DEV_GPU_TEMP` on
`(job, instance)`, deployment-agnostic.

---

## 80.6 Operational notes

- **The gpu-controller is not ArgoCD-managed** — rolling it is a manual
  `kubectl` operation.
- GPU-operator chart **v25.3.0+** (we pin v25.10.1) is required on
  containerd-2.x nodes; v24.9.x's toolkit cannot parse the version-3
  `config.toml`. On v25.10.1 the ClusterPolicy CRD requires env values to be
  strings — `CONTAINERD_SET_AS_DEFAULT` must go through `--set-string`
  (PR #111's GPU half; `deploy/k8s/gpu/00-install-gpu-operator.sh`).
- Example wiring: one GPU cluster's tunnel (e.g. `10.200.0.2`) fronts both inference
  ports — `:8001` the llama classifier (SOVEREIGN),
  `:8000` the coder (`qwen3-coder-30b-a3b`,
  `CODING_LOCAL_BASE_URL=http://10.200.0.2:8000/v1`). Remember the GPU
  exclusivity constraint when deciding what actually runs where — chapter 73
  covers the model catalog, sizing, and the port convention end-to-end.
