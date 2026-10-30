# GPU stack — workload-cluster GPU federated into the ops cluster

Manifests + install scripts for the local-first inference path described
in `docs/nvidia-strategy.md` Appendix B and operationalised in
`docs/local-gpu-and-billing.md`.

The stack is intentionally **fully open source** — no NVIDIA AI
Enterprise licence, no NGC NIM entitlement. The components:

* **NVIDIA driver** on the host (Ubuntu apt package, free).
* **nvidia-container-toolkit** on the host (apt, Apache 2.0 / MIT).
* **Standalone nvidia-device-plugin DaemonSet** (Apache 2.0). The full
  GPU Operator is *not* used on the workload cluster — it doesn't
  cleanly integrate with CRI-O 1.35 (see Appendix B). The
  device-plugin DaemonSet is the minimum that advertises
  `nvidia.com/gpu` to kubelet with time-slicing.
* **vLLM** (Apache 2.0, Docker Hub) serving an open Llama 3.1 8B
  Instruct AWQ checkpoint. OpenAI-compatible HTTP API on :8000.

```
deploy/k8s/gpu/
├── README.md                        — you are here
├── 00-install-gpu-operator.sh       — Operator install for containerd clusters (legacy)
├── 01-time-slicing.yaml             — Operator-mode ConfigMap (legacy; see Appendix B)
├── 02-hf-token.example.yaml         — HF token secret skeleton (NEVER commit real token)
├── 03-vllm-llama-classifier.yaml    — vLLM Deployment + Service + PDB (classifier tier)
├── 04-tenant-quota.example.yaml     — namespace + ResourceQuota template per tenant
├── 05-network-policy.yaml           — locks vLLM pod egress to cluster + HF only
├── 06-kueue-flavors.example.yaml    — Resource flavors + queues for batch (P1)
└── 10-vllm-qwen-coder-14b.yaml      — vLLM coder stack (qwen-coder/llm-coder) for the coding assistant
```

`00-install-gpu-operator.sh` is kept for clusters that *do* run
containerd (kubeadm, EKS, etc.). On the workload cluster (CRI-O)
this script is **skipped** — the standalone device-plugin DaemonSet
performs the same `nvidia.com/gpu` registration without the
Operator's containerd-specific machinery. See `docs/nvidia-strategy.md`
Appendix B for the exact device-plugin DaemonSet manifest and the
reasoning.

## Apply order (workload cluster, CRI-O — current setup)

Once the host driver + nvidia-container-toolkit are installed on the
GPU node and the standalone device-plugin DaemonSet is applied (see
Appendix B §B.4–B.5), the application-level pieces are:

```bash
export KUBECONFIG=~/.kube/workload-cluster

# 1. HF token secret (one-time per cluster)
kubectl create namespace daalu --dry-run=client -o yaml | kubectl apply -f -
kubectl -n daalu create secret generic hf-token \
  --from-literal=token="$HF_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

# 2. vLLM deployment + Service
kubectl apply -f 03-vllm-llama-classifier.yaml

# 3. NetworkPolicy
kubectl apply -f 05-network-policy.yaml

# 4. (Optional, P1) per-tenant quota + Kueue flavors
kubectl apply -f 04-tenant-quota.example.yaml   # template; copy per tenant
kubectl apply -f 06-kueue-flavors.example.yaml  # only after Kueue is installed
```

The whole flow is automated by `scripts/deploy-gpu.sh`.

## Coding-assistant model (vLLM coder stack)

The browser-IDE coding assistant (engineer chapters 35/36/73) serves a
dedicated coder model, named `qwen-coder` (Deployment) / `llm-coder`
(Service) so it can sit beside the classifier. The bundle is generated
from the model catalog — the single source of truth:

```bash
# Regenerate the committed manifest (the served name, HF repo,
# quantization and context length all come from core/model_catalog.py):
daalu render-coding-model qwen2.5-coder-14b --gpu-class ada-16 \
  > deploy/k8s/gpu/10-vllm-qwen-coder-14b.yaml
```

**The GPU is allocated exclusively to one pod.** On the 16 GB RTX 2000
Ada the coder cannot co-reside with the classifier — scale the
classifier to 0 first (the prod classifier runs on DeepSeek, so the card
is normally free):

```bash
export KUBECONFIG=~/.kube/workload-cluster
kubectl -n daalu scale deploy/llama-classifier --replicas=0   # free the card
kubectl -n daalu create secret generic hf-token \
  --from-literal=token="$HF_TOKEN" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f 10-vllm-qwen-coder-14b.yaml

# Verify the served model name matches the catalog (and DAALU_MODEL):
kubectl -n daalu port-forward svc/llm-coder 8000:80 &
curl -s localhost:8000/v1/models | jq '.data[].id'   # qwen/qwen2.5-coder-14b-instruct
```

Wire the hub: point `DAALU_HOSTED_UPSTREAM_URL` at the coder Service over
the WireGuard tunnel (e.g. `http://10.200.0.x:8000`) and add the catalog
id to `CODING_MODELS_ENABLED`. The 48 GB `ada-48` models
(`qwen2.5-coder-32b`, `qwen3-coder-30b-a3b`) render the same way once the
RTX 6000 Ada node is labeled `gpu-class=ada-48`.

## Apply order (kubeadm/EKS/k3s with containerd — legacy GPU-Operator path)

If you're bringing up a fresh cluster that *does* run containerd and
prefer the Operator's batteries-included setup (DCGM exporter,
GPU Feature Discovery, MIG manager all in one Helm release):

```bash
bash 00-install-gpu-operator.sh                  # Operator helm install
kubectl apply -f 01-time-slicing.yaml            # 4-way logical GPUs on Ada
kubectl patch clusterpolicy/cluster-policy -n gpu-operator --type merge -p '{
  "spec": { "devicePlugin": { "config": { "name": "time-slicing-config", "default": "ada-16" } } }
}'
kubectl label node <gpu-node> nvidia.com/device-plugin.config=ada-16 --overwrite

# then continue with 02–05 as above
```

## Repointing the app

The Daalu API uses two settings (see `src/daalu_automation/config.py`):

```env
LLM_LOCAL_BASE_URL=http://10.200.0.2:8000/v1
LLM_LOCAL_MODEL_CLASSIFIER=meta/llama-3.1-8b-instruct
```

The vLLM Deployment passes `--served-model-name=meta/llama-3.1-8b-instruct`
on purpose, so the model id the router sends is the same id vLLM
accepts — no app-side rename needed when migrating from NIM.

The router in `core/llm.py` health-checks the URL every 30 s and falls
back to the external classifier or Anthropic on miss — no app restart
required when the GPU node bounces.
