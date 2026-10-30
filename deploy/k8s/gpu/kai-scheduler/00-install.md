# KAI Scheduler — install pointer

[KAI Scheduler](https://github.com/NVIDIA/KAI-Scheduler) (`NVIDIA/KAI-Scheduler`,
Apache-2.0, CNCF Sandbox — the open-sourced Run:ai scheduling core) runs as a
**secondary scheduler** alongside the default `kube-scheduler`. Only pods that
explicitly set `schedulerName: kai-scheduler` are placed by it; system pods and
the daalu control plane stay on the default scheduler, so the blast radius of a
young scheduler is GPU workloads only. It is the GPU-native multi-tenancy engine
for the shared operator card — see
[`docs/plans/nvidia-ai-factory/06-kai-scheduler.md`](../../../../docs/plans/nvidia-ai-factory/06-kai-scheduler.md).

> **Not adopted yet.** The trigger is **>1 tenant sharing one card** (doc 13
> go-live). Until then the operator is the only tenant and whole-GPU scheduling
> on the default scheduler is fine.

## Prerequisites

- **GPU Operator** already advertising `nvidia.com/gpu` (Daalu runs v25.10.1 — a
  KAI prerequisite).
- **MPS, not time-slicing, on the shared card.** KAI's GPU fractions are
  MPS-backed. Flip the shared RTX 6000 Ada (`ada-48`) to MPS via the GPU
  Operator (`deploy/k8s/gpu/07-mps-config.yaml`, profile `ada-48-mps`); leave
  SOVEREIGN / non-shared cards on their existing time-slicing mode.
- Images mirrored to Harbor (Apache-2.0, self-pullable — no NGC entitlement,
  [[foss_inference_pivot]]).

## Install (Helm)

```bash
# Mirror first (no NGC entitlement needed):
#   harbor.daalu.io/nvidia/kai-scheduler/*  <-  ghcr.io/nvidia/kai-scheduler/*
helm upgrade --install kai-scheduler \
  oci://harbor.daalu.io/nvidia/kai-scheduler/kai-scheduler \
  --version v0.15.0 \
  --namespace kai-scheduler --create-namespace \
  --set global.registry=harbor.daalu.io/nvidia/kai-scheduler \
  --set gpuSharing.enabled=true \
  --set scheduler.placementStrategy.gpu=binpack \
  --set scheduler.placementStrategy.cpu=binpack
```

Key values:

| Value | Setting | Why |
|---|---|---|
| `global.registry` | `harbor.daalu.io/nvidia/kai-scheduler` | FOSS Harbor mirror, no NGC pull |
| `gpuSharing.enabled` | `true` | turn on reservation-based GPU fractions (MPS-backed) |
| scheduler name | `kai-scheduler` | the value pods set as `schedulerName` to opt in |
| `scheduler.placementStrategy.gpu` | `binpack` | pack fractions to free whole cards for big jobs |

> The deployed **scheduler name is `kai-scheduler`** — that exact string is what
> the vLLM Deployment's `schedulerName` must match. Setting that `schedulerName`
> on a cluster *without* KAI strands pods **Pending** forever, so the controller
> only emits it on a `kai_enabled` cluster (see below).

## Then apply the queues

After install, apply the Queue tree (parent `operator-shared` + per-tenant
children): [`10-queues.example.yaml`](10-queues.example.yaml). The
`gpu_controller` becomes the owner of the per-tenant Queue objects in prod.

## schedulerName + PodGroup wiring lives in code (not here)

The `schedulerName: kai-scheduler` + queue-label + `gpu_fraction` wiring on the
vLLM Deployment is added by the **main agent** in
`src/daalu_automation/gpu_controller/manifests.py` (`build_vllm_deployment` /
`GpuParams`), gated on a per-cluster `kai_enabled` flag — see
[`06-kai-scheduler.md`](../../../../docs/plans/nvidia-ai-factory/06-kai-scheduler.md)
§4.3. PodGrouper auto-creates the gang `PodGroup` from the Deployment, so no
explicit `PodGroup` object is needed for the single-replica vLLM case.
