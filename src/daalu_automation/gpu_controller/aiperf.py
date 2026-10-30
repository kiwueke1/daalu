"""Build + parse AIPerf load-test runs, and exec them on the operator cluster.

AIPerf (``ai-dynamo/aiperf``, Apache-2.0) is a pure OpenAI-compatible load
generator. It needs no operator/CRD/scheduler — it opens connections to a
Service that already exists and reports TTFT / ITL / throughput vs concurrency
(``docs/plans/nvidia-ai-factory/04-aiperf.md``). So this module is, like
``diagnostics``, pure helpers (arg builder, Job manifest, output parser) that
are unit-tested; the gpu-controller reconcile loop creates the Job, reads the
pod log, and writes back the parsed summary.

The Job ships in ``deploy/k8s/gpu/aiperf-bench-job.yaml`` as a hand-applied
template; this builder is the programmatic equivalent the UI drives.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Pinned + Harbor-mirrored before first real use ([[foss_inference_pivot]] — no
# NGC entitlement). Override per-cluster via settings.gpu_aiperf_image.
AIPERF_IMAGE = "harbor.daalu.io/ai-dynamo/aiperf:latest"

# MinIO client image for the artifact-uploader sidecar. Override via
# settings.gpu_aiperf_uploader_image (mirror to Harbor before first use).
UPLOADER_IMAGE = "minio/mc:latest"

# In-pod paths shared between the aiperf container and the uploader sidecar.
_ARTIFACT_DIR = "/artifacts"
# A tiny second volume the aiperf container drops a DONE sentinel into when the
# sweep finishes (pass or fail); the uploader waits on it before pushing, so it
# never races a half-written artifact tree.
_SIGNAL_DIR = "/signal"

# Default in-cluster target — today's vLLM (llama-classifier) Service. The
# gateway front-door is benchmarked by repointing target_url at the
# inference-gateway (and the UI flags it via_gateway).
DEFAULT_TARGET_URL = "http://llm-classifier.daalu.svc.cluster.local:80"
DEFAULT_MODEL = "meta/llama-3.1-8b-instruct"
DEFAULT_CONCURRENCY = "1,2,4,8,16,32"

# Bound the sweep so a UI-kicked run can't accidentally hammer the shared prod
# card for hours — a sane ceiling, not the off-peak policy (that's operational).
_MAX_REQUEST_COUNT = 2000
_MAX_TOKENS = 8192
_CONCURRENCY_RE = re.compile(r"^\s*\d+(\s*,\s*\d+)*\s*$")


def normalise_concurrency(concurrency: str | None) -> str:
    """Validate + canonicalise the comma-separated concurrency sweep."""
    c = (concurrency or "").strip()
    if not c or not _CONCURRENCY_RE.match(c):
        return DEFAULT_CONCURRENCY
    # Drop spaces, dedupe-preserving-order, cap each level to a sane ceiling.
    levels: list[str] = []
    for part in c.split(","):
        n = int(part)
        if n <= 0:
            continue
        n = min(n, 1024)
        s = str(n)
        if s not in levels:
            levels.append(s)
    return ",".join(levels) if levels else DEFAULT_CONCURRENCY


def aiperf_args(
    *,
    model: str,
    url: str,
    concurrency: str,
    request_count: int,
    input_tokens: int,
    output_tokens: int,
    endpoint_type: str = "chat",
    streaming: bool = True,
    auth_token: str | None = None,
) -> list[str]:
    """The argv AIPerf runs for a profiling sweep (matches aiperf-bench.sh)."""
    args = [
        "profile",
        f"--model={model}",
        f"--url={url}",
        f"--endpoint-type={endpoint_type}",
    ]
    if streaming:
        # Streaming is required to measure TTFT + ITL, not just total latency.
        args.append("--streaming")
    args += [
        f"--concurrency={normalise_concurrency(concurrency)}",
        f"--request-count={max(1, min(int(request_count), _MAX_REQUEST_COUNT))}",
        # AIPerf 0.9.0 renamed the token-count flags: the synthetic input length
        # is ``--synthetic-input-tokens-mean`` and the requested output length is
        # ``--output-tokens-mean`` (the bare ``--synthetic-input-tokens`` /
        # ``--output-tokens`` forms were removed). ``-stddev`` defaults to 0, so
        # a mean alone yields a fixed ISL/OSL.
        f"--synthetic-input-tokens-mean={max(1, min(int(input_tokens), _MAX_TOKENS))}",
        f"--output-tokens-mean={max(1, min(int(output_tokens), _MAX_TOKENS))}",
        "--artifact-dir=/artifacts",
    ]
    if auth_token:
        args.append(f"--header=Authorization: Bearer {auth_token}")
    return args


def _aiperf_container(args: list[str], image: str | None, *, with_upload: bool) -> dict:
    """The aiperf client container.

    When an uploader sidecar is present we wrap the run in a tiny ``python3``
    launcher so we can drop a DONE sentinel for the uploader to wait on — the
    sweep's exit code is preserved so a failed run still fails the pod. We use
    ``python3`` (not ``/bin/sh``) because the AIPerf image is shell-less: it
    ships ``python3`` and the ``aiperf`` console script but has no ``/bin/sh``.
    Args are passed as a JSON array in argv to sidestep all shell quoting (the
    auth header carries spaces/colons). Without an uploader (no S3 configured)
    the image entrypoint is invoked directly with bare ``args``.
    """
    base = {
        "name": "aiperf",
        "image": image or AIPERF_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "volumeMounts": [{"name": "artifacts", "mountPath": _ARTIFACT_DIR}],
        # CPU/network-bound client — explicitly no GPU.
        "resources": {
            "requests": {"cpu": "1", "memory": "1Gi"},
            "limits": {"cpu": "2", "memory": "2Gi"},
        },
    }
    if not with_upload:
        base["args"] = args
        return base
    base["volumeMounts"].append({"name": "signal", "mountPath": _SIGNAL_DIR})
    launcher = (
        "import json,subprocess,sys;"
        'rc=subprocess.call(["aiperf"]+json.loads(sys.argv[1]));'
        f'open("{_SIGNAL_DIR}/EXIT","w").write(str(rc));'
        f'open("{_SIGNAL_DIR}/DONE","w").close();'
        "sys.exit(rc)"
    )
    base["command"] = ["python3", "-c", launcher, json.dumps(args)]
    return base


def _uploader_container(image: str, s3: dict[str, str]) -> dict:
    """Sidecar that waits for the sweep to finish, then pushes the artifact tree
    to object storage under ``{prefix}/`` via the MinIO client (``mc``)."""
    # Wait (bounded) for the DONE sentinel, then mirror /artifacts to the
    # bucket. Tolerant: a missing bucket is created; cp failures don't wedge
    # the pod (the run's stdout is still captured by the controller).
    script = (
        f"i=0; until [ -f {_SIGNAL_DIR}/DONE ]; do "
        f"i=$((i+1)); [ $i -gt 1700 ] && break; sleep 2; done; "
        'mc alias set t "$S3_ENDPOINT" "$S3_KEY" "$S3_SECRET" >/dev/null 2>&1; '
        'mc mb --ignore-existing "t/$S3_BUCKET" >/dev/null 2>&1; '
        f'mc cp --recursive {_ARTIFACT_DIR}/ "t/$S3_BUCKET/$S3_PREFIX/" || true; '
        f": > {_SIGNAL_DIR}/UPLOADED"
    )
    return {
        "name": "artifact-uploader",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["/bin/sh", "-c", script],
        "env": [
            {"name": "S3_ENDPOINT", "value": s3["endpoint"]},
            {"name": "S3_KEY", "value": s3["access_key"]},
            {"name": "S3_SECRET", "value": s3["secret_key"]},
            {"name": "S3_BUCKET", "value": s3["bucket"]},
            {"name": "S3_PREFIX", "value": s3["prefix"]},
        ],
        "volumeMounts": [
            {"name": "artifacts", "mountPath": _ARTIFACT_DIR},
            {"name": "signal", "mountPath": _SIGNAL_DIR},
        ],
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "500m", "memory": "256Mi"},
        },
    }


def aiperf_bench_job(
    namespace: str,
    *,
    model: str,
    url: str,
    concurrency: str,
    request_count: int,
    input_tokens: int,
    output_tokens: int,
    endpoint_type: str = "chat",
    streaming: bool = True,
    auth_token: str | None = None,
    name: str = "daalu-aiperf",
    image: str | None = None,
    artifacts_s3: dict[str, str] | None = None,
    uploader_image: str | None = None,
) -> dict[str, Any]:
    """One-shot Job that runs an AIPerf sweep against ``url``.

    Pure client — requests **no GPU** (the GPU is the thing under test, on a
    different pod/node). The controller creates it, reads the pod log, and
    deletes it. ``backoffLimit: 0`` because a load test is not idempotent.

    When ``artifacts_s3`` is given (``{endpoint, access_key, secret_key, bucket,
    prefix}``) a second container is added that mirrors the AIPerf artifact tree
    (``profile_export_aiperf.csv/json`` + logs, per ISL/OSL/concurrency) to
    object storage after the sweep — so the run's structured outputs survive the
    pod and can be downloaded.
    """
    args = aiperf_args(
        model=model,
        url=url,
        concurrency=concurrency,
        request_count=request_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        endpoint_type=endpoint_type,
        streaming=streaming,
        auth_token=auth_token,
    )
    with_upload = artifacts_s3 is not None
    containers = [_aiperf_container(args, image, with_upload=with_upload)]
    volumes: list[dict] = [{"name": "artifacts", "emptyDir": {}}]
    if with_upload:
        containers.append(
            _uploader_container(uploader_image or UPLOADER_IMAGE, artifacts_s3)
        )
        volumes.append({"name": "signal", "emptyDir": {}})

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": "daalu-aiperf", "daalu.io/tier": "bench"},
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 600,
            "activeDeadlineSeconds": 3600,
            "template": {
                "metadata": {
                    "labels": {"app": "daalu-aiperf", "daalu.io/tier": "bench"}
                },
                "spec": {
                    "restartPolicy": "Never",
                    "containers": containers,
                    "volumes": volumes,
                },
            },
        },
    }


# ── output parsing ─────────────────────────────────────────────────────────
#
# AIPerf prints a human metrics table to stdout and writes a JSON report into
# the artifact dir. We capture stdout (the pod log), so parse the table; also
# try JSON in case a future version echoes the report. The parser is
# deliberately tolerant — it never raises, and ``passed`` means "we extracted
# at least a throughput or TTFT figure" (i.e. the run produced a usable curve),
# not a pass/fail verdict (a benchmark has none).

# Canonical metric → the substrings AIPerf / GenAI-Perf use for its row label.
_METRIC_LABELS: list[tuple[str, tuple[str, ...]]] = [
    ("ttft_ms", ("time to first token", "ttft")),
    ("itl_ms", ("inter token latency", "inter-token latency", "itl")),
    ("tpot_ms", ("time per output token", "tpot")),
    ("request_latency_ms", ("request latency",)),
    ("output_token_throughput", ("output token throughput", "output tokens per second")),
    ("request_throughput", ("request throughput", "requests per second")),
]

_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _first_number(s: str) -> float | None:
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_aiperf_output(stdout: str) -> tuple[bool, dict[str, Any]]:
    """Parse AIPerf stdout → (passed, summary).

    ``summary`` carries the headline metrics we could extract plus a raw tail.
    ``passed`` is true when at least one throughput/latency headline was found.
    """
    text = (stdout or "").strip()
    if not text:
        return False, {"error": "no output from AIPerf"}

    metrics: dict[str, float] = {}

    # 1) JSON form (if a version echoes the report). Walk for known keys.
    try:
        doc = json.loads(text)
        _collect_json_metrics(doc, metrics)
    except (ValueError, TypeError):
        pass

    # 2) Human table form — scan each line for a known metric label + its first
    #    numeric column (AIPerf's first stat column is the average).
    if not metrics:
        for raw in text.splitlines():
            low = raw.lower()
            for key, needles in _METRIC_LABELS:
                if key in metrics:
                    continue
                if any(n in low for n in needles):
                    # Take the number AFTER the label so we don't pick up a unit
                    # embedded in the label (e.g. "(ms)").
                    tail = raw[max(low.find(n) for n in needles if n in low) :]
                    # Skip the label text itself, then grab the first stat.
                    after = re.sub(r"^[^0-9|]*", "", tail)
                    val = _first_number(after)
                    if val is not None:
                        metrics[key] = val

    passed = bool(metrics)
    summary: dict[str, Any] = {"metrics": metrics} if metrics else {}
    if not passed:
        summary["error"] = "AIPerf produced no parseable metrics"
    summary["raw_tail"] = text[-4000:]
    return passed, summary


def _collect_json_metrics(node: Any, out: dict[str, float]) -> None:
    """Best-effort scrape of metric figures from an AIPerf JSON report."""
    if isinstance(node, dict):
        for k, v in node.items():
            # AIPerf/GenAI-Perf JSON keys use underscores ("time_to_first_token");
            # normalise to spaces so the same label needles match both forms.
            kl = str(k).lower().replace("_", " ")
            for key, needles in _METRIC_LABELS:
                if key in out:
                    continue
                if any(n in kl for n in needles):
                    val = _extract_avg(v)
                    if val is not None:
                        out[key] = val
            _collect_json_metrics(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect_json_metrics(item, out)


def _extract_avg(v: Any) -> float | None:
    """Pull an average/value out of a metric node (dict with avg, or scalar)."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("avg", "average", "mean", "value", "p50"):
            if isinstance(v.get(k), (int, float)):
                return float(v[k])
    return None


# ── per-concurrency curve from the uploaded artifact tree ──────────────────
#
# AIPerf 0.9.0 mirrors a tree keyed by concurrency level:
# ``concurrency_{n}/profile_export_aiperf.json`` (+ csv/jsonl/logs) per sweep
# level, plus ``aggregate/concurrency_{n}/...aggregate.json`` and
# ``sweep_aggregate/...sweep.json`` roll-ups. ISL/OSL are no longer in the path
# (they're flags now) — each per-level JSON carries ``input_sequence_length`` /
# ``output_sequence_length`` instead. We parse one row per per-level
# ``profile_export_aiperf.json`` so the UI can plot the SLO curve (latency /
# throughput vs concurrency) rather than just the last summary table. The
# legacy ``CON{n}`` / ``ISL{n}_OSL{n}`` GenAI-Perf layout is still recognised.

# 0.9.0 ``concurrency_10`` first, then legacy ``CON10`` — the leading ``_``/``/``
# boundary keeps it from matching inside ``prefill-concurrency`` etc.
_CON_RE = re.compile(r"(?:concurrency[_-]|con)(\d+)", re.IGNORECASE)
_ISL_RE = re.compile(r"ISL(\d+)", re.IGNORECASE)
_OSL_RE = re.compile(r"OSL(\d+)", re.IGNORECASE)


def parse_aiperf_artifacts(files: dict[str, bytes]) -> list[dict[str, Any]]:
    """Build a per-concurrency curve from the uploaded artifact tree.

    ``files`` maps a *relative* artifact path (e.g.
    ``concurrency_10/profile_export_aiperf.json``) to its bytes. Returns
    ``[{concurrency, isl, osl, metrics, path}]`` sorted by concurrency, one row
    per per-level ``profile_export_aiperf.json``. The ``aggregate`` / ``sweep``
    roll-ups (``*_aggregate.json`` / ``*_sweep.json``) are skipped by the exact
    suffix match. Tolerant — skips unparseable files.
    """
    rows: list[dict[str, Any]] = []
    for path, blob in files.items():
        if not path.endswith("profile_export_aiperf.json"):
            continue
        try:
            doc = json.loads(blob.decode("utf-8", "replace"))
        except (ValueError, TypeError):
            continue
        metrics: dict[str, float] = {}
        _collect_json_metrics(doc, metrics)
        con = _CON_RE.search(path)
        # ISL/OSL come from the path on the legacy layout, else from the doc's
        # input/output_sequence_length (0.9.0 puts the lengths in the report).
        isl = _ISL_RE.search(path)
        osl = _OSL_RE.search(path)
        isl_val = int(isl.group(1)) if isl else _seq_len(doc, "input_sequence_length")
        osl_val = int(osl.group(1)) if osl else _seq_len(doc, "output_sequence_length")
        rows.append(
            {
                "concurrency": int(con.group(1)) if con else None,
                "isl": isl_val,
                "osl": osl_val,
                "metrics": metrics,
                "path": path,
            }
        )
    rows.sort(key=lambda r: (r["concurrency"] is None, r["concurrency"] or 0))
    return rows


def _seq_len(doc: Any, key: str) -> int | None:
    """Pull the average input/output sequence length (rounded) from a report."""
    if isinstance(doc, dict):
        avg = _extract_avg(doc.get(key))
        if avg is not None:
            return round(avg)
    return None
