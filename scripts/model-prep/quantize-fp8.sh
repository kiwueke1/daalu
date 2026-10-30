#!/usr/bin/env bash
# Offline FP8 quantization of a Llama/Qwen HF checkpoint with NVIDIA Model
# Optimizer, producing an HF-format checkpoint that vLLM serves directly.
# See docs/plans/nvidia-ai-factory/05-model-optimizer.md.
#
# This is a *batch job that makes a file*, NOT a runtime service: it runs
# once per checkpoint, on a GPU node, and disappears. The serving plane is
# unchanged — once the FP8 checkpoint exists, serving it is a single field
# change (GpuParams.hf_model -> the FP8 repo + the matching vLLM quant flag).
#
#   full / HF checkpoint --(Model Optimizer FP8)--> HF-format FP8 checkpoint
#       --> [eval gate] --> [AIPerf throughput check] --> push to Harbor / HF mirror
#
# HARD RULE — Ada is FP8, never NVFP4. The RTX 6000 Ada (sm_89) has FP8
# tensor cores but NO FP4/NVFP4 (Blackwell-only). An NVFP4 checkpoint will
# not execute on Ada — this script refuses QFORMAT=nvfp4.
#
# PREFER NVIDIA'S PUBLISHED FP8 FIRST. For Llama-3.1-8B and the Qwen2.5
# family NVIDIA already publishes FP8 checkpoints on Hugging Face — mirror
# and serve those (zero quantization compute) and only run this for a model
# NVIDIA hasn't pre-quantized or a custom accuracy/throughput tradeoff.
#
# Usage:
#   scripts/model-prep/quantize-fp8.sh \
#     --src meta-llama/Meta-Llama-3.1-8B-Instruct \
#     --out /data/models/llama-3.1-8b-fp8
#
# Flags / env (flags win):
#   --src <hf-repo|path>   Source checkpoint (HF repo id or local path). REQUIRED.
#   --out <dir>            Output checkpoint dir (host path). REQUIRED.
#   --qformat <fp8>        Quant format. Default: fp8. nvfp4 is REJECTED on Ada.
#   --calib-size <n>       Calibration samples for activation scales. Default: 512.
#   --image <ref>          Model Optimizer image. Default: MODELOPT_IMAGE or
#                          harbor.daalu.io/nvidia/modelopt:latest (Harbor mirror).
#   --hf-token <token>     HF token for gated source repos. Default: HF_TOKEN env.
#
# Status: SKELETON. The exact modelopt CLI/entrypoint and flags must be
# pinned against the chosen Model Optimizer image before first real run; the
# `modelopt` invocation below is the documented shape, not a verified command.

set -euo pipefail

SRC=""
OUT=""
QFORMAT="fp8"
CALIB_SIZE=512
IMAGE="${MODELOPT_IMAGE:-harbor.daalu.io/nvidia/modelopt:latest}"
HF_TOKEN_VAL="${HF_TOKEN:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --src)        SRC="$2"; shift 2 ;;
    --out)        OUT="$2"; shift 2 ;;
    --qformat)    QFORMAT="$2"; shift 2 ;;
    --calib-size) CALIB_SIZE="$2"; shift 2 ;;
    --image)      IMAGE="$2"; shift 2 ;;
    --hf-token)   HF_TOKEN_VAL="$2"; shift 2 ;;
    -h|--help)    sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done

[[ -n "${SRC}" ]] || { echo "ERROR: --src is required" >&2; exit 1; }
[[ -n "${OUT}" ]] || { echo "ERROR: --out is required" >&2; exit 1; }

# Ada hard rule: refuse NVFP4. FP8 is the terminal quant for this generation.
case "${QFORMAT}" in
  fp8) ;;
  nvfp4|fp4)
    echo "ERROR: QFORMAT='${QFORMAT}' is Blackwell-only and will NOT run on the" >&2
    echo "       RTX 6000 Ada (sm_89). Use --qformat fp8. See 05-model-optimizer.md." >&2
    exit 1 ;;
  *) echo "ERROR: unsupported --qformat '${QFORMAT}' (expected: fp8)" >&2; exit 1 ;;
esac

mkdir -p "${OUT}"
OUT_ABS="$(cd "${OUT}" && pwd)"

echo "==> Model Optimizer FP8 quantization"
echo "    src     : ${SRC}"
echo "    qformat : ${QFORMAT}"
echo "    calib   : ${CALIB_SIZE} samples"
echo "    out     : ${OUT_ABS}"
echo "    image   : ${IMAGE}"
echo

RUNTIME="docker"
command -v docker >/dev/null 2>&1 || RUNTIME="podman"
command -v "${RUNTIME}" >/dev/null 2>&1 || {
  echo "ERROR: need docker or podman on PATH." >&2; exit 1
}

# Containerized quantization. --gpus all: quantization needs the card during
# calibration (off-peak / candidate node only, same rule as the AIPerf load
# test). --export-fmt hf so the output loads natively in vLLM.
#
# NOTE: the `modelopt` subcommand/flags below track the documented Model
# Optimizer quantization shape; pin them to the image's actual CLI before a
# production run. This is the skeleton 05-model-optimizer.md §3.1 describes.
"${RUNTIME}" run --rm \
  --gpus all \
  -e HF_TOKEN="${HF_TOKEN_VAL}" \
  -v "${OUT_ABS}:/out" \
  "${IMAGE}" \
  modelopt quantize \
    --model "${SRC}" \
    --qformat "${QFORMAT}" \
    --calib-size "${CALIB_SIZE}" \
    --export-fmt hf \
    --output-dir /out

echo
echo "==> FP8 checkpoint written to ${OUT_ABS}"
echo
echo "BEFORE SERVING — the mandatory two-part eval gate (05-model-optimizer.md §5):"
echo "  1. Accuracy: lm-eval-harness on the classifier/quality tasks; must stay"
echo "     within tolerance of the source checkpoint. Fail -> do not ship."
echo "  2. Throughput: scripts/aiperf-bench.sh on the FP8 checkpoint must beat"
echo "     the AWQ-INT4 incumbent at equal SLO, else there is no reason to switch."
echo
echo "On pass: push ${OUT_ABS} to Harbor / an HF mirror, then point"
echo "GpuParams.hf_model at it (vLLM serves the HF-format FP8 checkpoint directly)."
