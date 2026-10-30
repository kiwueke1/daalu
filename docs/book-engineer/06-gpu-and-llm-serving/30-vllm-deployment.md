# 30. vLLM on the workload cluster

The vLLM Helm chart at `deploy/helm/daalu-vllm/` deploys an
OpenAI-compatible inference server on a GPU node in the
customer's cluster.

This chapter covers the chart's structure and rationale.

---

## What the chart deploys

- **A Deployment** running the vLLM image (with the chosen
  model).
- **A Service** exposing port 8000 to the cluster (and via
  federation tunnel to Daalu's router).
- **A PVC** (optional) for pre-staged weights.
- **A ServiceMonitor** for Prometheus scraping of vLLM's
  `/metrics`.
- **An optional ConfigMap** for prompt-format overrides.

---

## Values

`values.yaml`:

```yaml
model:
  name: hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4
  servedAs: meta/llama-3.1-8b-instruct
  tensorParallelism: 1
  maxModelLen: 8192
  trust_remote_code: false
  localPath: ""    # if set, use pre-staged weights

hf:
  token: ""        # Hugging Face token for gated models

resources:
  limits:
    nvidia.com/gpu: 1
    memory: 24Gi
  requests:
    cpu: "4"
    memory: 16Gi

service:
  type: ClusterIP
  port: 8000

monitoring:
  enabled: true
  release: kube-prom-stack  # label so the SM is discovered
```

`servedAs` is the model name exposed via vLLM's API. The
LLM router uses this to match local capabilities against
incoming requests.

---

## NVIDIA path

The pod requires `nvidia.com/gpu: 1` (or more, for tensor-
parallel). The customer cluster must have the NVIDIA device
plugin installed.

On CRI-O clusters, the standalone device plugin pattern
applies — see Chapter 32.

---

## Startup

vLLM's startup is slow on first start because it downloads
weights from Hugging Face. Expected times:

- 8B INT4 (~4 GB): 1–3 min.
- 70B INT4 (~40 GB): 10–30 min.

Pre-staging weights on a PVC eliminates this. The chart
supports it via `model.localPath`.

The pod's `readinessProbe` polls `/health` on port 8000.
Once it returns 200, vLLM is ready.

---

## OpenAI-compatible API

vLLM serves:

- `GET /v1/models` — list available models.
- `POST /v1/chat/completions` — chat-style API.
- `POST /v1/completions` — completion-style API.
- `POST /v1/embeddings` — embeddings (if model supports).
- `GET /metrics` — Prometheus metrics.

The LLM router uses the same client code for local and
cloud — it just swaps base URLs.

---

## Reaching the pod from Daalu

The router constructs:

```
http://<tunnel-ip>:8000/v1/chat/completions
```

The tunnel IP is the cluster's; in-cluster the Service
resolves to the pod. The router uses an httpx async client
with a 60-second timeout (configurable).

---

## Multiple replicas

For HA / throughput, replicas can be >1. With multiple
replicas:

- Each pod has its own model loaded in GPU memory.
- Each pod requires its own GPU.
- The Service distributes requests round-robin.

For most customers, 1 replica is plenty.

---

## Metrics

vLLM exposes operationally-useful metrics:

- `vllm:request_count_total` — total request counter.
- `vllm:request_success_total` — success counter.
- `vllm:request_failure_total` — failure counter.
- `vllm:e2e_request_latency_seconds` — end-to-end latency
  histogram.
- `vllm:gpu_cache_usage_perc` — KV cache utilization.
- `vllm:num_requests_running` — current in-flight requests.
- `vllm:num_requests_waiting` — queue depth.

Daalu's router uses `num_requests_running` to make the
queue-depth spillover decision.

---

## Operational gotchas

A few we've hit:

- **VRAM fragmentation.** vLLM benefits from a fresh restart
  every few weeks to reclaim KV-cache fragmentation. We bake
  in a once-a-month rollout restart for long-running
  customers.
- **Driver mismatch.** vLLM requires a specific CUDA/NVIDIA
  driver matrix. The image's CUDA version must match the
  host's driver major.
- **GSP firmware hangs.** On some RTX 2000 Ada cards, rapid
  device churn (start/stop) causes GSP firmware to hang.
  Recovery requires a hardware power cycle. We treat this
  hardware as fragile; the chart pins to a stable node and
  doesn't reschedule aggressively.

---

## What's next

Chapter 31 — the LLM router internals.
