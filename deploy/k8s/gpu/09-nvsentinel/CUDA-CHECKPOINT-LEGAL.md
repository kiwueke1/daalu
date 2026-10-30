# cuda-checkpoint — LEGAL SIGN-OFF GATE (do not ship without it)

`cuda-checkpoint` is NVIDIA's tool to checkpoint/restore the CUDA state of a
running process (via CRIU). It is the lever behind two future payoffs (doc 03
§2): fault-tolerant GPU jobs, and near-zero vLLM cold starts / faster
tenant-switch on a shared card.

## Why this file exists

> **cuda-checkpoint is PROPRIETARY NVIDIA software (NVIDIA EULA), not
> Apache-2.0/BSD.** It is **free to use** but is **NOT FOSS** and is an explicit
> exception to the [[foss_inference_pivot]] hard rule. **Legal sign-off is
> required before any redistribution or productisation** — in particular before
> bundling it into a Harbor-mirrored image we ship to customer clusters. Using
> it internally / experimentally is lower risk than shipping it.

Because of this, cuda-checkpoint is **adopt-later / experimental**, sequenced
after NVSentinel and after legal review — not a launch dependency (doc 03 §2.1,
Phase P5).

## The code gate

There is **no cuda-checkpoint code path in the product.** The only hook is a
hard-off feature flag:

- `settings.gpu_cuda_checkpoint_enabled` — default **False**.
- The AI-factory **Reliability** panel renders it as **"gated — legal sign-off
  required"** while false, and never offers a checkpoint/restore action.

Do **not** flip this flag, mirror the cuda-checkpoint binary into Harbor, or add
a checkpoint/restore feature until **all** of the following are true:

1. **Legal sign-off** on the NVIDIA EULA for our intended use (internal-only vs
   shipped-to-customer is a material distinction — get the scope right).
2. A Harbor mirror entry is approved for the specific pinned version.
3. The first use is **internal/experimental only** (a vLLM warm-restore PoC) and
   restored state is treated as suspect until validated against a cold-start
   baseline (doc 03 §4 "checkpoint correctness").

## Checklist before enabling

- [ ] Legal has signed off on the EULA for the intended distribution scope.
- [ ] The pinned version + Harbor mirror entry are approved.
- [ ] CRIU is present and handles the workload's non-GPU state.
- [ ] Same-arch restore only (homogeneous node; no cross-card restore goal).
- [ ] A warm-restore PoC shows materially lower tenant-switch latency than cold
      load (doc 03 acceptance #6) before any further investment.
