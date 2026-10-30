"""Catalog of coding-assistant models — the single source of truth.

A workspace user picks a model in the Create-Workspace UI. That choice
has to agree, byte-for-byte, across five places or inference silently
404s:

    catalog.served_model_name
      == vLLM ``--served-model-name`` (gpu_controller.manifests.GpuParams)
      == the ``DAALU_MODEL`` env injected into the code-server container
      == the ``model`` field the pod sends to the gateway
      == (later, multi-pool) ``GpuPool.served_models[]``

So everything downstream derives the served name (and the HF repo,
quantization, and target GPU class) from a *catalog entry* rather than
re-typing the string. This module is deliberately pure data — it imports
nothing from elsewhere in the package, so daalu-api, the
workspace-controller, and the gpu-controller can all import it without a
circular dependency.

GPU classes (node label ``gpu-class``):

* ``ada-16`` — RTX 2000 Ada, 16 GB. Fits a 14B AWQ-INT4 coder as the
  *sole* vLLM on the card (the device plugin allocates a GPU exclusively
  to one pod; 8B + 14B do not co-fit in 16 GB).
* ``ada-48`` — RTX 6000 Ada, 48 GB. Fits a 32B dense coder (AWQ) or the
  Qwen3-Coder-30B-A3B MoE (FP8).

Which catalog ids are *actually deployed* (and therefore selectable) is
governed by ``settings.coding_models_enabled`` — not by this file. A
model can be listed here (config-ready) long before its GPU arrives.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CodingModel:
    """One selectable coding model and everything needed to serve it."""

    id: str
    label: str
    served_model_name: str
    gpu_class: str  # "ada-16" | "ada-48"
    hf_model: str
    quantization: str  # vLLM --quantization; "" lets vLLM infer (e.g. FP8 checkpoints)
    vram_gb: int  # minimum card size the model targets
    context_len: int  # --max-model-len we launch with
    description: str
    default: bool = False
    # vLLM tool-call parser for OpenAI function-calling. When set, the
    # rendered Deployment gets ``--enable-auto-tool-choice
    # --tool-call-parser=<this>`` so the model can drive the in-IDE agent's
    # tools natively instead of us regex-parsing text pragmas. The Qwen2.5
    # family uses the Hermes parser; Qwen3-Coder ships a dedicated
    # ``qwen3_coder`` parser. Empty = no tool-calling (chat-only serving).
    tool_call_parser: str = ""


# Ordered: the default first, then by ascending GPU size. The UI renders
# in this order.
_CATALOG: tuple[CodingModel, ...] = (
    CodingModel(
        id="qwen2.5-coder-14b",
        label="Qwen2.5-Coder 14B",
        served_model_name="qwen/qwen2.5-coder-14b-instruct",
        gpu_class="ada-16",
        hf_model="Qwen/Qwen2.5-Coder-14B-Instruct-AWQ",
        quantization="awq_marlin",
        vram_gb=16,
        context_len=8192,
        description=(
            "Strong open coding model that fits a 16 GB RTX 2000 Ada. "
            "AWQ-INT4 weights (~9-10 GB); the everyday default."
        ),
        default=True,
        tool_call_parser="hermes",
    ),
    CodingModel(
        id="qwen2.5-coder-32b",
        label="Qwen2.5-Coder 32B",
        served_model_name="qwen/qwen2.5-coder-32b-instruct",
        gpu_class="ada-48",
        hf_model="Qwen/Qwen2.5-Coder-32B-Instruct-AWQ",
        quantization="awq_marlin",
        vram_gb=48,
        # Native context is 32768. AWQ-INT4 weights (~18 GB) leave ~24 GB of
        # the 48 GB card for KV cache (~100K tokens total) — enough for 32K
        # context across a few concurrent workspaces. (131072 via YaRN is
        # possible but trades concurrency; revisit per load.)
        context_len=32768,
        description=(
            "The dense 32B coder — best single-model code quality at this "
            "size. Needs a 48 GB RTX 6000 Ada."
        ),
        tool_call_parser="hermes",
    ),
    CodingModel(
        id="qwen3-coder-30b-a3b",
        label="Qwen3-Coder 30B-A3B",
        served_model_name="qwen/qwen3-coder-30b-a3b-instruct",
        gpu_class="ada-48",
        hf_model="Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8",
        quantization="",  # FP8 checkpoint — let vLLM infer
        vram_gb=48,
        # MoE: ~3B active params → fast decode, and the FP8 weights (~30 GB)
        # still leave room for a longer window than the dense 32B. Native
        # context is huge (262144); we launch at 65536 as a practical 48 GB
        # default (KV-cache-bounded at low concurrency) — the agentic loop
        # benefits most from the extra context, and this is the agent model.
        context_len=65536,
        description=(
            "Newest Qwen3-Coder (MoE, 30B total / 3B active) — very fast, "
            "long-context, purpose-built for agentic tool use. FP8 on a "
            "48 GB RTX 6000 Ada."
        ),
        tool_call_parser="qwen3_coder",
    ),
)

_BY_ID: dict[str, CodingModel] = {m.id: m for m in _CATALOG}


def all_models() -> tuple[CodingModel, ...]:
    """Every catalog entry, in display order."""
    return _CATALOG


def get(model_id: str) -> CodingModel | None:
    """Look up a model by id, or None if unknown."""
    return _BY_ID.get(model_id)


def default_model() -> CodingModel:
    """The catalog default (the one pre-selected in the UI)."""
    for m in _CATALOG:
        if m.default:
            return m
    return _CATALOG[0]


def is_servable(model_id: str, enabled_ids: list[str] | set[str]) -> bool:
    """True if ``model_id`` is both a real catalog entry and enabled.

    ``enabled_ids`` is ``settings.coding_models_enabled`` — the ids whose
    vLLM is actually deployed on a present GPU.
    """
    return model_id in _BY_ID and model_id in set(enabled_ids)


def served_name(model_id: str) -> str:
    """Resolve a catalog id to its vLLM served-model-name.

    Falls back to the default model's served name for an unknown id so a
    stale ``DAALU_MODEL`` never hands the pod an empty model string.
    """
    m = _BY_ID.get(model_id)
    return (m or default_model()).served_model_name


# ── Classifier (SRE-agent) models ────────────────────────────────────────
#
# The tenant's own-GPU model — the one alert triage, briefings and the ops
# chat run on through ``llm.chat_with_tools``. Keyed by served-model-name
# because that is what ``GpuTenant.model_classifier`` and
# ``Tenant.sovereign_model_classifier`` store; the gpu-controller looks the
# entry up to render the vLLM Deployment. An unknown name falls back to the
# legacy Llama defaults in ``gpu_controller.manifests`` so existing rows keep
# rendering byte-for-byte what they did before this catalog existed.
#
# Model choice and sizing rationale: see the model catalog entries below.


@dataclass(frozen=True, slots=True)
class ClassifierModel:
    """One servable SRE-agent model and everything needed to serve it."""

    served_model_name: str
    hf_model: str
    quantization: str  # "" lets vLLM infer (FP8 checkpoints)
    context_len: int
    # vLLM tool-call parser. Triage *requires* tool calls, so every entry
    # meant for the agent should set one; empty = chat-only (legacy Llama).
    tool_call_parser: str = ""
    # Fraction of the card when this model has the GPU to itself. A tenant
    # sharing the card (time-slicing) overrides it per row
    # (``GpuTenant.gpu_memory_utilization``).
    gpu_memory_utilization: float = 0.90
    extra_args: tuple[str, ...] = ()
    mem_request: str = "12Gi"
    mem_limit: str = "20Gi"


_CLASSIFIERS: tuple[ClassifierModel, ...] = (
    ClassifierModel(
        served_model_name="meta/llama-3.1-8b-instruct",
        hf_model="hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4",
        quantization="awq_marlin",
        context_len=8192,
    ),
    ClassifierModel(
        served_model_name="qwen/qwen3.6-35b-a3b",
        # MoE, 35B total / 3B active, Apache-2.0. FP8 weights are ~35 GiB, so
        # on a 48 GB card the KV cache is what's left: FP8 KV + a 32k window
        # keeps it inside ~0.85 of the card. The vision tower is never used
        # for triage, so no image/video inputs are profiled or accepted.
        hf_model="Qwen/Qwen3.6-35B-A3B-FP8",
        quantization="",
        context_len=32768,
        tool_call_parser="qwen3_coder",
        # 0.85, not 0.90: the card is time-sliced and the lab's chat-llm holds
        # 0.10. A reconnect that lost the per-tenant override (a fresh
        # gpu_tenants row) must still fit, or vLLM refuses to start.
        gpu_memory_utilization=0.85,
        extra_args=(
            "--reasoning-parser=qwen3",
            "--kv-cache-dtype=fp8",
            "--max-num-seqs=8",
            '--limit-mm-per-prompt={"image":0,"video":0}',
        ),
        mem_request="16Gi",
        mem_limit="48Gi",
    ),
)

_CLASSIFIER_BY_NAME: dict[str, ClassifierModel] = {
    m.served_model_name: m for m in _CLASSIFIERS
}


def classifier(served_model_name: str) -> ClassifierModel | None:
    """Look up an SRE-agent model by served name, or None if unknown."""
    return _CLASSIFIER_BY_NAME.get(served_model_name)
