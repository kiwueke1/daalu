"""Unit tests for the gpu-controller manifest builders.

Pure functions — no cluster, DB, or network needed. They guard that the
in-code port of ``deploy/k8s/gpu/*`` stays faithful and parameterizes
correctly.
"""

from __future__ import annotations

import base64

from daalu_automation.core import model_catalog
from daalu_automation.gpu_controller import manifests as m


def _by_kind(bundle):
    return {x["kind"]: x for x in bundle}


def test_build_all_has_expected_kinds_in_order():
    bundle = m.build_all(m.GpuParams(hf_token="hf_xxx"))
    kinds = [x["kind"] for x in bundle]
    assert kinds == [
        "Namespace",
        "Secret",
        "Deployment",
        "Service",
        "PodDisruptionBudget",
        "NetworkPolicy",
    ]
    # Namespace + Secret must precede the Deployment that references them.
    assert kinds.index("Namespace") < kinds.index("Deployment")
    assert kinds.index("Secret") < kinds.index("Deployment")


def test_namespace_is_threaded_everywhere():
    bundle = m.build_all(m.GpuParams(namespace="tenant-x"))
    for obj in bundle:
        if obj["kind"] == "Namespace":
            assert obj["metadata"]["name"] == "tenant-x"
        else:
            assert obj["metadata"]["namespace"] == "tenant-x"


def test_hf_token_is_base64_encoded():
    secret = _by_kind(m.build_all(m.GpuParams(hf_token="hf_secret123")))["Secret"]
    assert (
        base64.b64decode(secret["data"]["token"]).decode() == "hf_secret123"
    )


def test_empty_hf_token_yields_empty_secret_value():
    secret = _by_kind(m.build_all(m.GpuParams()))["Secret"]
    assert base64.b64decode(secret["data"]["token"]).decode() == ""


def test_served_model_name_drives_vllm_arg():
    dep = _by_kind(m.build_all(m.GpuParams(served_model_name="meta/foo")))[
        "Deployment"
    ]
    args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--served-model-name=meta/foo" in args


def test_gpu_class_drives_node_selector():
    dep = _by_kind(m.build_all(m.GpuParams(gpu_class="ada-48")))["Deployment"]
    assert dep["spec"]["template"]["spec"]["nodeSelector"] == {
        "gpu-class": "ada-48"
    }


def test_deployment_requests_one_gpu_and_recreate_strategy():
    dep = _by_kind(m.build_all(m.GpuParams()))["Deployment"]
    assert dep["spec"]["strategy"]["type"] == "Recreate"
    res = dep["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert res["limits"]["nvidia.com/gpu"] == 1
    assert res["requests"]["nvidia.com/gpu"] == 1


def test_service_maps_80_to_8000_and_selects_deployment():
    svc = _by_kind(m.build_all(m.GpuParams()))["Service"]
    port = svc["spec"]["ports"][0]
    assert port["port"] == m.SERVICE_PORT == 80
    assert port["targetPort"] == m.VLLM_PORT == 8000
    assert svc["spec"]["selector"]["app"] == m.DEPLOYMENT_NAME
    assert svc["metadata"]["name"] == m.SERVICE_NAME == "llm-classifier"


def test_max_model_len_and_mem_util_are_parameterized():
    dep = _by_kind(
        m.build_all(m.GpuParams(max_model_len=4096, gpu_memory_utilization=0.8))
    )["Deployment"]
    args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--max-model-len=4096" in args
    assert "--gpu-memory-utilization=0.8" in args


def _args(params):
    dep = _by_kind(m.build_all(params))["Deployment"]
    return dep["spec"]["template"]["spec"]["containers"][0]["args"]


def test_tool_call_parser_emits_function_calling_flags():
    args = _args(m.GpuParams(tool_call_parser="hermes"))
    assert "--enable-auto-tool-choice" in args
    assert "--tool-call-parser=hermes" in args


def test_no_tool_call_parser_omits_flags():
    args = _args(m.GpuParams())  # default empty parser
    assert "--enable-auto-tool-choice" not in args
    assert not any(a.startswith("--tool-call-parser=") for a in args)


def test_coder_params_from_catalog_threads_model_and_tool_parser():
    # Qwen3-Coder MoE: FP8 (no --quantization), qwen3_coder parser, ada-48
    # sizing, long context — all derived from the catalog entry.
    entry = model_catalog.get("qwen3-coder-30b-a3b")
    assert entry is not None
    params = m.coder_params_from_catalog(entry, hf_token="hf_x")
    assert params.served_model_name == "qwen/qwen3-coder-30b-a3b-instruct"
    assert params.tool_call_parser == "qwen3_coder"
    assert params.gpu_class == "ada-48"
    assert params.deployment_name == "qwen-coder"
    assert params.service_name == "llm-coder"
    # ada-48 bumps the host-RAM envelope for the larger checkpoint.
    assert params.mem_limit == "32Gi"
    args = _args(params)
    assert "--tool-call-parser=qwen3_coder" in args
    assert "--max-model-len=65536" in args
    # FP8 checkpoint → quantization inferred, so no flag.
    assert not any(a.startswith("--quantization=") for a in args)


def test_coder_params_14b_is_ada16_hermes_awq():
    entry = model_catalog.get("qwen2.5-coder-14b")
    assert entry is not None
    params = m.coder_params_from_catalog(entry)
    assert params.gpu_class == "ada-16"
    assert params.mem_limit == "20Gi"
    args = _args(params)
    assert "--quantization=awq_marlin" in args
    assert "--tool-call-parser=hermes" in args


def _args(params: m.GpuParams) -> list[str]:
    return m.build_vllm_deployment(params)["spec"]["template"]["spec"][
        "containers"
    ][0]["args"]


def test_image_is_pinned_not_latest():
    assert not m.DEFAULT_IMAGE.endswith(":latest")
    assert m.build_vllm_deployment(m.GpuParams())["spec"]["template"]["spec"][
        "containers"
    ][0]["image"] == m.DEFAULT_IMAGE


def test_classifier_params_unknown_name_keeps_legacy_llama_defaults():
    p = m.classifier_params("some/unlisted-model")
    assert p.served_model_name == "some/unlisted-model"
    assert p.hf_model == m.DEFAULT_HF_MODEL
    assert p.gpu_memory_utilization == 0.90
    assert p.tool_call_parser == ""


def test_classifier_params_llama_renders_as_before():
    legacy = _args(m.GpuParams())
    assert _args(m.classifier_params("meta/llama-3.1-8b-instruct")) == legacy


def test_classifier_params_qwen36_threads_catalog_entry():
    spec = model_catalog.classifier("qwen/qwen3.6-35b-a3b")
    assert spec is not None
    p = m.classifier_params(spec.served_model_name, namespace="daalu")
    args = _args(p)
    assert f"--model={spec.hf_model}" in args
    assert "--served-model-name=qwen/qwen3.6-35b-a3b" in args
    assert not any(a.startswith("--quantization") for a in args)  # FP8 inferred
    assert "--tool-call-parser=qwen3_coder" in args
    assert "--enable-auto-tool-choice" in args
    assert "--reasoning-parser=qwen3" in args
    assert f"--max-model-len={spec.context_len}" in args


def test_classifier_params_row_override_beats_catalog_mem_util():
    p = m.classifier_params(
        "qwen/qwen3.6-35b-a3b", gpu_memory_utilization=0.80
    )
    assert "--gpu-memory-utilization=0.8" in _args(p)
    p = m.classifier_params("qwen/qwen3.6-35b-a3b", gpu_memory_utilization=None)
    assert "--gpu-memory-utilization=0.85" in _args(p)


def test_qwen36_reconnect_renders_exactly_what_production_runs():
    """A Disconnect + Connect (fresh gpu_tenants row, no override) must
    reproduce the live SRE-agent stack on cp03 arg for arg — at 0.90 it
    collided with the lab's 0.10 chat-llm on the time-sliced card."""
    p = m.classifier_params("qwen/qwen3.6-35b-a3b", gpu_memory_utilization=None)
    assert _args(p) == [
        "--model=Qwen/Qwen3.6-35B-A3B-FP8",
        "--served-model-name=qwen/qwen3.6-35b-a3b",
        "--max-model-len=32768",
        "--gpu-memory-utilization=0.85",
        "--host=0.0.0.0",
        "--port=8000",
        "--enable-auto-tool-choice",
        "--tool-call-parser=qwen3_coder",
        "--reasoning-parser=qwen3",
        "--kv-cache-dtype=fp8",
        "--max-num-seqs=8",
        '--limit-mm-per-prompt={"image":0,"video":0}',
    ]
