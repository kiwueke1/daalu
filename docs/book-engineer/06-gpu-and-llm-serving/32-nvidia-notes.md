# 32. NVIDIA-specific notes

The GPU side of Daalu is one of the parts that has bitten us
the most. This chapter is the running notebook of what we've
learned. If you're deploying or debugging GPU workloads, read
it.

---

## Driver / runtime / device-plugin matrix

For vLLM to work, you need (in order):

1. NVIDIA host driver installed on the node.
2. Container runtime that can pass GPUs into containers
   (containerd-nvidia or CRI-O with nvidia-container-runtime).
3. Kubernetes device plugin (advertises `nvidia.com/gpu` to
   the scheduler).
4. The vLLM pod's image with matching CUDA libraries.

If any link is wrong, the pod either schedules but can't
access the GPU, or doesn't schedule at all.

---

## Containerd path (the easy case)

Most Kubernetes distros use containerd. The standard path:

```bash
# install the device plugin
helm install nvidia-device-plugin oci://nvcr.io/nvidia/k8s-device-plugin
```

Verify nodes advertise GPUs:

```bash
kubectl get nodes -o yaml | grep nvidia.com/gpu
```

Should see `nvidia.com/gpu: "1"` in capacity.

---

## CRI-O path (the harder case)

Some distros (RHEL, Talos, some k3s variants) use CRI-O
instead of containerd. The NVIDIA GPU Operator's default
pathway expects containerd and doesn't work cleanly.

The fix: use the **standalone device-plugin DaemonSet**, not
the GPU Operator. Key flags:

```bash
deploy-gpu.sh --skip-host-prep --skip-operator
```

This installs only the device plugin without trying to
install the (incompatible) operator components.

---

## GSP firmware hangs

We've hit this on RTX 2000 Ada cards: rapid /dev/nvidia*
churn (e.g., pod restarts in quick succession) causes the
GPU's GSP firmware to hang. The pod then can't access the
GPU; even `nvidia-smi` on the host hangs.

Recovery: hard power cycle of the host. A reboot does not
fix it; the firmware state survives reboots.

Mitigation:

- Pin the vLLM pod to a stable node, don't reschedule on
  every change.
- Avoid rapid rollout cycles; queue updates.
- We use Rufio BMC management for the hard power cycle when
  needed.

---

## FOSS pivot

Originally we tried to use NVIDIA NIM (NVIDIA's hosted
inference microservices). It was deeply problematic:

- Required NVAIE entitlements (paid NVIDIA program).
- NGC pulls were 403 from anywhere outside an entitled
  account.
- Locked us into NVIDIA's catalog of models.

We pivoted to vLLM serving open-weight models (Llama, Qwen)
directly. This is a hard rule:
no NVAIE, no NIM, no NGC.

---

## DCGM exporter

For GPU-side observability:

```bash
helm install nvidia-dcgm-exporter \
  oci://nvcr.io/nvidia/k8s/dcgm-exporter \
  --namespace nvidia-device-plugin
```

DCGM exporter exposes per-GPU metrics on `/metrics`:

- `DCGM_FI_DEV_GPU_UTIL` — utilization %.
- `DCGM_FI_DEV_FB_USED` — frame buffer used (VRAM).
- `DCGM_FI_DEV_POWER_USAGE` — power draw.
- `DCGM_FI_DEV_GPU_TEMP` — temperature.

A ServiceMonitor in `deploy/k8s/monitoring/03-servicemonitors.yaml`
declares `nvidia-dcgm` as a scrape job. Note that the ServiceMonitor
expects port `gpu-metrics` on the Service.

---

## Multi-GPU and tensor parallelism

vLLM supports tensor parallelism via the `tensor_parallel_
size` setting. For 70B on 2× A100 40GB:

```yaml
model:
  tensorParallelism: 2
resources:
  limits:
    nvidia.com/gpu: 2
```

Constraints:

- The two GPUs must be on the same node.
- They should be the same model (mixing causes weird perf).
- NVLink between them helps a lot; PCIe-only is OK but slower.

---

## Time slicing

For testing on a single GPU, time-slicing lets multiple pods
share it:

```yaml
# device-plugin ConfigMap
sharing:
  timeSlicing:
    resources:
    - name: nvidia.com/gpu
      replicas: 4
```

Each pod thinks it has the GPU; the scheduler interleaves
them. Throughput drops linearly; latency suffers; only useful
for dev/test, not production.

---

## What's next

Chapter 33 — the classifier, which is a smaller LLM running
on the GPU node for the routing decisions covered in Chapter
31.
