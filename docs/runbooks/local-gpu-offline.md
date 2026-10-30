# LocalGpuOffline

> The local vLLM has been unhealthy for 5 minutes.

## Why it matters

Graceful degradation is doing its job — the router falls back to the
external classifier (or Anthropic) automatically — but every minute
the GPU node is down, we pay external rates instead of marginal-cost
rates. After ~30 min that meaningfully moves the cost-per-call line.

## Investigate

```bash
# Is the pod up?
kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n daalu \
  get pod -l app=llama-classifier -o wide

# What does the pod think?
kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n daalu \
  logs -l app=llama-classifier --tail=200

# Is the node Ready and advertising nvidia.com/gpu?
kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" get node <gpu-node> \
  -o jsonpath='{.status.capacity.nvidia\.com/gpu}{"\n"}'

# Is the WG tunnel up? (run on the GPU host)
sudo wg show wg0
```

## Resolve

**Pod CrashLoopBackOff with CUDA OOM** — VRAM saturated. Lower
`--gpu-memory-utilization` in `deploy/k8s/gpu/03-vllm-llama-classifier.yaml`
from `0.90` to `0.85`, or reduce `--max-model-len`. If you've added
co-resident models (embeddings, reranker), scale time-slicing back
from 4-way to 2-way:

```bash
kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n nvidia-device-plugin \
  edit cm time-slicing-config
# ada-16 → replicas: 2
# then bounce the device-plugin DaemonSet:
kubectl --kubeconfig "$WORKLOAD_KUBECONFIG" -n nvidia-device-plugin \
  rollout restart ds/nvidia-device-plugin
```

**Pod stuck in `ContainerCreating`** — usually a `nvidia.com/gpu`
allocation race (kubelet sees the request before the device-plugin
finishes registering). `kubectl describe pod` will show the exact
reason. If the device-plugin pod itself is missing, re-apply its
DaemonSet manifest (see book-engineer chapter 32, NVIDIA notes).

**Pod stuck downloading from HF** — first start needs ~5 GB from
`huggingface.co`. The model is in the `hf-token` secret's repo
(`hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4`). Check:
- Secret exists: `kubectl -n daalu get secret hf-token`
- Token still works: `curl -sI -H "Authorization: Bearer $HF_TOKEN" https://huggingface.co/api/whoami-v2`
- NetworkPolicy allows egress to public IPs:443 (it does, but verify it wasn't tightened)

**Node `NotReady`** — workload-cluster node lost connectivity or
power. From the GPU host:
```bash
sudo systemctl status crio kubelet
sudo systemctl restart kubelet     # only if it's actually stopped
```
If unreachable entirely, Rufio power-cycle (the GSP firmware on the
Ada is also vulnerable to wedging — see below).

**GPU wedged (GSP firmware hang)** — `nvidia-smi` reports "No devices
found" on the host, `dmesg` shows `kgspWaitForGfwBootOk_TU102: failed`.
Soft reset (rmmod / modprobe) does **not** recover this; PCIe
re-enumeration via a hard power-cycle is the only fix.

**Pod healthy but health probe still red** — the cache in
`core/llm._local_is_healthy` is 30 s. If `/health` succeeds manually
but the metric stays at 0, restart `daalu-api` once.

## Escalate

If the GPU node has been down >30 min and no one can power-cycle
the GPU node: flip every tenant on Local-First to External-Only temporarily
via the `/billing` admin tool. Note this for incident review —
manual flips should be rare.
