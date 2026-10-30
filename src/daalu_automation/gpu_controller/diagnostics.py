"""Build + parse GPU diagnostic commands, and exec them on the target cluster.

Pure helpers (command builders, output parsers, the nccl Job manifest) are
unit-tested; the actual ``kubectl exec`` / Job orchestration is cluster-gated
and runs from the gpu-controller reconcile loop, which already holds a K8s
client for the (operator or customer) cluster behind the WireGuard tunnel.

``dcgmi diag`` runs inside the **dcgm-exporter** pod (it bundles DCGM/NVVS).
``nccl-tests`` needs a multi-GPU Job and is only valid on a >1-GPU node.
"""

from __future__ import annotations

import json
from typing import Any

DCGM_POD_NAMESPACES = ("nvidia-device-plugin", "gpu-operator")
DCGM_POD_LABEL = "app=nvidia-dcgm-exporter"
# The dcgm-exporter pod's metrics container. The GPU-Operator names it
# "nvidia-dcgm-exporter" (NOT "exporter"); the exec must target it exactly.
DCGM_CONTAINER = "nvidia-dcgm-exporter"


def clamp_level(level: int | None) -> int:
    """dcgmi diag run-level — default 1, clamp to the valid 1..3 range."""
    if level is None:
        return 1
    return max(1, min(3, int(level)))


def dcgmi_diag_cmd(level: int | None) -> list[str]:
    """The exec argv for ``dcgmi diag`` at the given run-level, JSON output."""
    return ["dcgmi", "diag", "-r", str(clamp_level(level)), "-j"]


# Run diagnostics in a one-shot pod from the full DCGM image (cloud-native/dcgm),
# which bundles nv-hostengine, dcgmi AND the NVVS diagnostic plugins. Two things
# must line up or dcgmi diag fails:
#   * DCGM version vs GPU driver — too old → "Detected unsupported Cuda version".
#   * NVVS plugin set vs CUDA — the driver here is CUDA-13 era (595.x), so the
#     image must ship the cuda13 plugins (the dcgm-exporter image does NOT).
# 4.4.2-1-ubuntu22.04 carries DCGM 4.4.2 + cuda12/cuda13/cudaless plugins.
# Override per-cluster via settings.gpu_dcgm_diag_image. Mirror to Harbor in prod.
DCGM_DIAG_IMAGE = "nvcr.io/nvidia/cloud-native/dcgm:4.4.2-1-ubuntu22.04"


def dcgmi_diag_job(
    namespace: str,
    *,
    level: int | None,
    gpu_class: str | None,
    node: str | None,
    name: str = "daalu-dcgmi-diag",
    image: str | None = None,
) -> dict[str, Any]:
    """One-shot Job that runs ``dcgmi diag`` from the full DCGM image.

    Starts a local nv-hostengine, runs the diag at ``level`` with JSON output,
    requests a single GPU (a time-slice on this node), and self-cleans via
    ``ttlSecondsAfterFinished``. Pure manifest builder — the controller creates
    it, reads the pod log, and deletes it.
    """
    lvl = clamp_level(level)
    node_selector: dict[str, str] = {}
    if node:
        node_selector["kubernetes.io/hostname"] = node
    elif gpu_class:
        node_selector["gpu-class"] = gpu_class
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": "daalu-dcgmi-diag"},
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 600,
            "activeDeadlineSeconds": 1800,
            "template": {
                "metadata": {"labels": {"app": "daalu-dcgmi-diag"}},
                "spec": {
                    "restartPolicy": "Never",
                    **({"nodeSelector": node_selector} if node_selector else {}),
                    "tolerations": [
                        {"key": "gpu", "operator": "Exists", "effect": "NoSchedule"},
                        {
                            "key": "nvidia.com/gpu",
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        },
                    ],
                    "containers": [
                        {
                            "name": "dcgmi",
                            "image": image or DCGM_DIAG_IMAGE,
                            # nv-hostengine must be up before dcgmi connects.
                            "command": ["/bin/sh", "-c"],
                            "args": [
                                "nv-hostengine && sleep 2 && "
                                f"dcgmi diag -r {lvl} -j"
                            ],
                            "resources": {"limits": {"nvidia.com/gpu": 1}},
                        }
                    ],
                },
            },
        },
    }


# Run-level descriptions + whether an explicit user acknowledgement is required
# before we'll run it (because it puts real stress on the GPU). Level 1 is a
# quick software/PCIe/NVML pass (safe alongside live traffic); 2 and 3 run
# escalating stress workloads.
def diag_stress_warning(kind: str, level: int | None) -> str | None:
    """Return a human warning describing the GPU stress, or None if the run is
    light enough not to need an explicit acknowledgement."""
    if kind == "nccl_test":
        return (
            "NCCL test exercises the GPU-to-GPU interconnect at full bandwidth "
            "across every GPU on the node for a sustained period. It needs >1 GPU "
            "and puts heavy load on the cards and NVLink/PCIe fabric."
        )
    if kind != "dcgmi_diag":
        return None
    lvl = clamp_level(level)
    if lvl <= 1:
        return None  # quick, low-stress — no ack needed
    if lvl == 2:
        return (
            "dcgmi diag -r2 (medium, ~2-3 min) runs targeted stress: GPU memory, "
            "SM compute and PCIe-bandwidth tests. It needs several GB of free VRAM "
            "— this card is shared and vLLM reserves ~90% of it, so the run may "
            "fail to allocate and will contend with live inference while it runs. "
            "On a card with known firmware fragility this adds real risk."
        )
    return (
        "dcgmi diag -r3 (long, ~15+ min) runs the FULL stress suite: sustained "
        "memory, compute and thermal load at maximum. It will saturate the GPU, "
        "degrade or stall live inference, needs the card largely free of other "
        "VRAM users, and on a fragile card can trip a firmware hang that needs a "
        "hard power-cycle to recover. Run only on a drained card in a maintenance "
        "window."
    )


def parse_dcgmi_output(stdout: str) -> tuple[bool, dict[str, Any]]:
    """Parse dcgmi diag output → (passed, summary).

    Prefers the JSON form (``-j``); falls back to a text heuristic. ``passed``
    is true only when no subtest reports a failure.
    """
    text = (stdout or "").strip()
    if not text:
        return False, {"error": "no output from dcgmi diag"}

    # JSON form: walk results for any "result":"Fail".
    try:
        doc = json.loads(text)
        results: list[str] = []
        _collect_results(doc, results)
        if results:
            failed = [r for r in results if r.lower() in ("fail", "failed")]
            return (len(failed) == 0), {
                "checks": len(results),
                "failed": len(failed),
                "results": results[:50],
            }
    except (ValueError, TypeError):
        pass

    # Text fallback.
    low = text.lower()
    passed = ("fail" not in low) and ("pass" in low)
    return passed, {"raw_tail": text[-2000:]}


_VERDICTS = {"pass", "passed", "fail", "failed", "warn", "skip", "skipped"}


def _collect_results(node: Any, out: list[str]) -> None:
    # DCGM 3.x uses {"result": "Pass"}; 4.4.x uses {"test_summary": {"status":
    # "Fail"}} — accept both verdict keys (guarding by value so we don't pick up
    # unrelated "status" fields).
    if isinstance(node, dict):
        for k, v in node.items():
            if (
                k.lower() in ("result", "status")
                and isinstance(v, str)
                and v.lower() in _VERDICTS
            ):
                out.append(v)
            else:
                _collect_results(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect_results(item, out)


def nccl_all_reduce_job(namespace: str, *, gpu_class: str, gpus: int = 2) -> dict[str, Any]:
    """A one-shot Job running nccl-tests ``all_reduce_perf`` across ``gpus``.

    Pure manifest builder. Only meaningful on a multi-GPU node — the caller
    must gate on node GPU count > 1.
    """
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "nccl-test", "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {"labels": {"app": "nccl-test"}},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": {"gpu-class": gpu_class},
                    "tolerations": [
                        {"key": "gpu", "operator": "Exists", "effect": "NoSchedule"},
                    ],
                    "containers": [
                        {
                            "name": "nccl",
                            "image": "nvcr.io/nvidia/pytorch:24.10-py3",  # mirror to Harbor
                            "command": ["all_reduce_perf", "-b", "8", "-e", "256M", "-f", "2", "-g", str(gpus)],
                            "resources": {"limits": {"nvidia.com/gpu": gpus}},
                        }
                    ],
                },
            },
        },
    }


def nccl_eligible(gpu_count: int) -> bool:
    """nccl-tests measure a collective — only valid with >1 GPU."""
    return gpu_count > 1
