# model-prep — offline checkpoint quantization

Offline, batch model-prep that produces **FP8** Llama/Qwen checkpoints which
the existing `gpu_controller` serves **directly in vLLM** (no engine change).
This is the "make a file once" stage described in
[`docs/plans/nvidia-ai-factory/05-model-optimizer.md`](../../docs/plans/nvidia-ai-factory/05-model-optimizer.md)
— it runs *before* serving, on a GPU node, then disappears. It is **not** a
runtime service and is not in any request path.

## Hardware rule: Ada is FP8, never NVFP4

The card on order is an **RTX 6000 Ada (48 GB, `sm_89`)** — FP8 tensor cores,
**no MIG, no FP4/NVFP4** (NVFP4 is Blackwell-only). FP8 is the terminal quant
for the Ada generation. `quantize-fp8.sh` **refuses** `--qformat nvfp4`; an
NVFP4 checkpoint simply will not execute on Ada.

## Prefer NVIDIA's published FP8 first

For Llama-3.1-8B and the Qwen2.5 family, NVIDIA already publishes FP8
checkpoints on Hugging Face. The first move is to **mirror and serve those**
(zero quantization compute on our side) and only run the quantizer here for a
model NVIDIA hasn't pre-quantized, or for a custom accuracy/throughput
tradeoff. Either way the result is just a `hf_model` value for the controller.

## Scripts

| Script | Purpose |
|---|---|
| [`quantize-fp8.sh`](quantize-fp8.sh) | Containerized NVIDIA Model Optimizer FP8 quantization of an HF checkpoint → HF-format FP8 checkpoint dir. **Skeleton** — pin the `modelopt` CLI/flags to the chosen image before a production run. |

```bash
scripts/model-prep/quantize-fp8.sh \
  --src meta-llama/Meta-Llama-3.1-8B-Instruct \
  --out /data/models/llama-3.1-8b-fp8
```

## The mandatory eval gate (before serving any quantized checkpoint)

A faster-but-dumber model is a worse product. No quantized checkpoint is
served until it clears **both**:

1. **Accuracy** — lm-eval-harness on the classifier/quality tasks Daalu
   routes; must stay within tolerance of the source checkpoint.
2. **Throughput** — [`scripts/aiperf-bench.sh`](../aiperf-bench.sh) must show
   the FP8 checkpoint beats the AWQ-INT4 incumbent at equal SLO.

On pass, push the checkpoint to Harbor / an HF mirror
([[foss_inference_pivot]] — FOSS-only, no NGC) and point `GpuParams.hf_model`
at it. vLLM loads the HF-format FP8 checkpoint natively.

## Run it off-peak

Quantization calibration wants the GPU. Run on a candidate node or off-peak,
same rule as the AIPerf load test (04-aiperf §4.2) — never on a card actively
serving tenant traffic.
