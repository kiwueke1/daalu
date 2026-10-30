#!/usr/bin/env bash
# Ad-hoc AIPerf load test against an OpenAI-compatible endpoint.
#
# Wraps the ai-dynamo/aiperf container (Apache-2.0, Harbor-mirrored — no NGC
# entitlement) for low-ceremony runs: the RTX 6000 Ada acceptance gate, a
# quick FP8-vs-AWQ-INT4 comparison, or a one-off saturation sweep. The
# in-cluster equivalent is deploy/k8s/gpu/aiperf-bench-job.yaml; this is the
# laptop/CI path. See docs/plans/nvidia-ai-factory/04-aiperf.md.
#
# It prints AIPerf's TTFT (time-to-first-token), ITL (inter-token latency)
# and throughput (output tokens/sec, requests/sec) per concurrency level.
#
# Usage:
#   scripts/aiperf-bench.sh [flags]
#
# Flags / env (flags win):
#   --url <url>            Endpoint base URL. Default: AIPERF_URL or
#                          http://localhost:8000  (e.g. a kubectl port-forward
#                          of svc/llm-classifier, or the gateway URL).
#   --model <name>         Served-model-name to request. Default:
#                          meta/llama-3.1-8b-instruct.
#   --concurrency <list>   Comma list to sweep. Default: 1,2,4,8,16,32.
#   --request-count <n>    Requests per concurrency level. Default: 200.
#   --input-tokens <n>     Synthetic prompt length. Default: 512.
#   --output-tokens <n>    Max output tokens. Default: 256.
#   --endpoint-type <t>    chat | completions | embeddings. Default: chat.
#   --auth <token>         If set, sends `Authorization: Bearer <token>`
#                          (use when pointing --url at the inference-gateway).
#   --image <ref>          AIPerf image. Default: AIPERF_IMAGE or
#                          harbor.daalu.io/ai-dynamo/aiperf:latest.
#   --artifact-dir <dir>   Host dir to write the CSV/JSON report into.
#                          Default: ./aiperf-artifacts.
#
# WARNING: a full-concurrency sweep IS load. Against the single shared prod
# card, run off-peak or point --url at a candidate node before the router is
# flipped to it (04-aiperf.md §4.2).

set -euo pipefail

URL="${AIPERF_URL:-http://localhost:8000}"
MODEL="meta/llama-3.1-8b-instruct"
CONCURRENCY="1,2,4,8,16,32"
REQUEST_COUNT=200
INPUT_TOKENS=512
OUTPUT_TOKENS=256
ENDPOINT_TYPE="chat"
AUTH=""
IMAGE="${AIPERF_IMAGE:-harbor.daalu.io/ai-dynamo/aiperf:latest}"
ARTIFACT_DIR="./aiperf-artifacts"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)            URL="$2"; shift 2 ;;
    --model)          MODEL="$2"; shift 2 ;;
    --concurrency)    CONCURRENCY="$2"; shift 2 ;;
    --request-count)  REQUEST_COUNT="$2"; shift 2 ;;
    --input-tokens)   INPUT_TOKENS="$2"; shift 2 ;;
    --output-tokens)  OUTPUT_TOKENS="$2"; shift 2 ;;
    --endpoint-type)  ENDPOINT_TYPE="$2"; shift 2 ;;
    --auth)           AUTH="$2"; shift 2 ;;
    --image)          IMAGE="$2"; shift 2 ;;
    --artifact-dir)   ARTIFACT_DIR="$2"; shift 2 ;;
    -h|--help)        sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done

mkdir -p "${ARTIFACT_DIR}"
ARTIFACT_ABS="$(cd "${ARTIFACT_DIR}" && pwd)"

echo "==> AIPerf sweep"
echo "    endpoint : ${URL}  (${ENDPOINT_TYPE})"
echo "    model    : ${MODEL}"
echo "    sweep    : concurrency=${CONCURRENCY}  requests/level=${REQUEST_COUNT}"
echo "    tokens   : in=${INPUT_TOKENS} out=${OUTPUT_TOKENS}"
echo "    image    : ${IMAGE}"
echo "    artifacts: ${ARTIFACT_ABS}"
echo

# Assemble the aiperf args.
AIPERF_ARGS=(
  profile
  --model="${MODEL}"
  --url="${URL}"
  --endpoint-type="${ENDPOINT_TYPE}"
  --streaming
  --concurrency="${CONCURRENCY}"
  --request-count="${REQUEST_COUNT}"
  --synthetic-input-tokens-mean="${INPUT_TOKENS}"
  --output-tokens-mean="${OUTPUT_TOKENS}"
  --artifact-dir=/artifacts
)
if [[ -n "${AUTH}" ]]; then
  AIPERF_ARGS+=( --header="Authorization: Bearer ${AUTH}" )
fi

# Prefer docker; fall back to podman.
RUNTIME="docker"
command -v docker >/dev/null 2>&1 || RUNTIME="podman"
command -v "${RUNTIME}" >/dev/null 2>&1 || {
  echo "ERROR: need docker or podman on PATH to run the AIPerf container." >&2
  exit 1
}

# --network host so localhost / a kubectl port-forward on the host is
# reachable from inside the container.
"${RUNTIME}" run --rm \
  --network host \
  -v "${ARTIFACT_ABS}:/artifacts" \
  "${IMAGE}" \
  "${AIPERF_ARGS[@]}"

echo
echo "==> Done. TTFT / ITL / throughput summary above; full CSV/JSON in:"
echo "    ${ARTIFACT_ABS}"
echo "Feed the tokens/sec into docs/pricing/03-ai-and-gpu-token-pricing.md;"
echo "correlate with DCGM (02-dcgm-observability.md) for device-side saturation."
