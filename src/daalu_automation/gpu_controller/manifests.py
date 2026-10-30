"""Manifest builders for the per-tenant vLLM GPU stack.

A faithful, parameterized port of ``deploy/k8s/gpu/03-vllm-llama-classifier.yaml``
and ``05-network-policy.yaml`` plus the ``hf-token`` Secret + namespace —
returned as plain dicts so the reconcile loop can ``apply_one`` them onto
the operator's or a customer's cluster.

Keep this in sync with ``deploy/k8s/gpu/*`` (the manual path
``scripts/deploy-gpu.sh`` applies). We intentionally re-express the
manifests in code rather than parse the YAML so the controller can
parameterize ``gpu_class`` / model / HF token per tenant without a
templating dependency.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

# Service name is unchanged from the NIM era — the router's endpoint
# points here. Renaming would require an ops-side config flip.
SERVICE_NAME = "llm-classifier"
DEPLOYMENT_NAME = "llama-classifier"
VLLM_PORT = 8000
SERVICE_PORT = 80
HF_SECRET_NAME = "hf-token"
# Pinned, never ``latest``: pods use ``imagePullPolicy: IfNotPresent``, so a
# floating tag silently keeps whatever build a node cached first — too old for
# newer model architectures (Qwen3.6's hybrid attention needs a 2026 vLLM).
DEFAULT_IMAGE = "vllm/vllm-openai:v0.29.0"
DEFAULT_HF_MODEL = "hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4"


@dataclass
class GpuParams:
    """Desired state for one tenant's vLLM stack."""

    namespace: str = "daalu"
    gpu_class: str = "ada-16"
    # Resource names. Default to the classifier stack; override for a
    # second, co-resident stack (e.g. the coder: ``qwen-coder`` /
    # ``llm-coder``) so Deployment/Service/PDB/NetworkPolicy don't collide
    # in the same namespace.
    deployment_name: str = DEPLOYMENT_NAME
    service_name: str = SERVICE_NAME
    network_policy_name: str = "local-llm-egress"
    hf_token: str = ""
    # Served model id the router sends / vLLM advertises.
    served_model_name: str = "meta/llama-3.1-8b-instruct"
    # The upstream HF repo vLLM downloads.
    hf_model: str = DEFAULT_HF_MODEL
    image: str = DEFAULT_IMAGE
    max_model_len: int = 8192
    gpu_memory_utilization: float = 0.90
    # Serving engine (doc 10). ``vllm`` is the default and only built path
    # today; ``tensorrt_llm``/``triton`` are a per-tenant "performance tier"
    # selected by overriding ``image`` + ``extra_args`` for modern cards.
    # The value labels the workload (``daalu.io/engine``) and is the seam
    # for future per-engine arg builders — vLLM args are still emitted below.
    engine: str = "vllm"
    # vLLM ``--quantization``. ``awq_marlin`` (AWQ-INT4) is the default for
    # the 16 GB Ada; flip to ``fp8`` on the 48 GB RTX 6000 Ada (doc 05).
    # Empty string omits the flag (let vLLM infer from the checkpoint).
    quantization: str = "awq_marlin"
    # vLLM tool-call parser (OpenAI function-calling). When set, emit
    # ``--enable-auto-tool-choice --tool-call-parser=<this>`` so the served
    # model can drive the in-IDE agent's tools natively. ``hermes`` for the
    # Qwen2.5 family, ``qwen3_coder`` for Qwen3-Coder. Empty = chat-only.
    tool_call_parser: str = ""
    # Extra raw vLLM args appended verbatim (e.g. ``--max-num-seqs=256``).
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    # KAI hook (doc 06): when set, the pod runs under this secondary
    # scheduler so KAI's hierarchical fair-share applies. None = default
    # kube-scheduler.
    scheduler_name: str | None = None
    # Workload ``tenant`` label — defaults to the internal operator pool;
    # set to a tenant id/queue when KAI fair-share is in play.
    tenant_label: str = "operator-internal"
    # Container memory request/limit (host RAM, not VRAM). A 14B/32B coder
    # loads a larger checkpoint into page cache than the 8B classifier, so
    # the coder stacks bump these.
    mem_request: str = "12Gi"
    mem_limit: str = "20Gi"


def build_namespace(params: GpuParams) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": params.namespace},
    }


def build_hf_token_secret(params: GpuParams) -> dict[str, Any]:
    # Opaque secret with the HF read token. Empty token is allowed
    # (ungated/cached weights) but vLLM will fail to pull a gated repo.
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": HF_SECRET_NAME, "namespace": params.namespace},
        "type": "Opaque",
        "data": {
            "token": base64.b64encode(
                (params.hf_token or "").encode("utf-8")
            ).decode("ascii"),
        },
    }


def _vllm_args(params: GpuParams) -> list[str]:
    # quantization is optional (empty omits the flag so vLLM infers it from
    # the checkpoint, e.g. for FP8 weights); ``extra_args`` tunes batch/
    # context per gpu-class.
    args = [
        f"--model={params.hf_model}",
        f"--served-model-name={params.served_model_name}",
    ]
    if params.quantization:
        args.append(f"--quantization={params.quantization}")
    args += [
        f"--max-model-len={params.max_model_len}",
        f"--gpu-memory-utilization={params.gpu_memory_utilization}",
        "--host=0.0.0.0",
        f"--port={VLLM_PORT}",
    ]
    if params.tool_call_parser:
        # OpenAI function-calling: let the model emit ``tool_calls`` the
        # in-IDE agent loop executes. Non-tool chat requests are unaffected.
        args += [
            "--enable-auto-tool-choice",
            f"--tool-call-parser={params.tool_call_parser}",
        ]
    args += list(params.extra_args)
    return args


def build_vllm_deployment(params: GpuParams) -> dict[str, Any]:
    pod_spec: dict[str, Any] = {
        "tolerations": [
            {
                "key": "node-role.kubernetes.io/control-plane",
                "operator": "Exists",
                "effect": "NoSchedule",
            },
            {"key": "gpu", "operator": "Exists", "effect": "NoSchedule"},
            {
                "key": "node.kubernetes.io/unreachable",
                "operator": "Exists",
                "effect": "NoExecute",
                "tolerationSeconds": 1800,
            },
        ],
        "nodeSelector": {"gpu-class": params.gpu_class},
        "containers": [
            {
                "name": "vllm",
                "image": params.image,
                "imagePullPolicy": "IfNotPresent",
                "ports": [{"containerPort": VLLM_PORT, "name": "http"}],
                "args": _vllm_args(params),
                "env": [
                    {
                        "name": "HUGGING_FACE_HUB_TOKEN",
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": HF_SECRET_NAME,
                                "key": "token",
                            }
                        },
                    },
                    {
                        "name": "HF_HOME",
                        "value": "/var/cache/huggingface",
                    },
                ],
                "resources": {
                    "limits": {
                        "nvidia.com/gpu": 1,
                        "memory": params.mem_limit,
                    },
                    "requests": {
                        "nvidia.com/gpu": 1,
                        "memory": params.mem_request,
                        "cpu": 2,
                    },
                },
                "volumeMounts": [
                    {
                        "name": "hf-cache",
                        "mountPath": "/var/cache/huggingface",
                    },
                    {"name": "dshm", "mountPath": "/dev/shm"},
                ],
                "startupProbe": {
                    "httpGet": {"path": "/health", "port": "http"},
                    "failureThreshold": 60,
                    "periodSeconds": 10,
                },
                "readinessProbe": {
                    "httpGet": {"path": "/health", "port": "http"},
                    "initialDelaySeconds": 60,
                    "periodSeconds": 10,
                },
                "livenessProbe": {
                    "httpGet": {"path": "/health", "port": "http"},
                    "initialDelaySeconds": 300,
                    "periodSeconds": 30,
                    "failureThreshold": 3,
                },
            }
        ],
        "volumes": [
            {
                "name": "hf-cache",
                "hostPath": {
                    "path": "/var/lib/daalu/hf-cache",
                    "type": "DirectoryOrCreate",
                },
            },
            {
                "name": "dshm",
                "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"},
            },
        ],
    }
    # KAI (doc 06): run under the secondary scheduler when requested.
    if params.scheduler_name:
        pod_spec["schedulerName"] = params.scheduler_name

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": params.deployment_name,
            "namespace": params.namespace,
            "labels": {
                "app": params.deployment_name,
                "daalu.io/tier": "local",
                "daalu.io/engine": params.engine,
            },
        },
        "spec": {
            "replicas": 1,
            # VRAM is stateful — RollingUpdate would double the GPU memory
            # requirement during a rollout. Recreate until there is >1 GPU
            # per gpu-class.
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": params.deployment_name}},
            "template": {
                "metadata": {
                    "labels": {
                        "app": params.deployment_name,
                        "daalu.io/tier": "local",
                        "daalu.io/engine": params.engine,
                        "tenant": params.tenant_label,
                    }
                },
                "spec": pod_spec,
            },
        },
    }


def build_service(params: GpuParams) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": params.service_name,
            "namespace": params.namespace,
            "annotations": {
                "prometheus.io/scrape": "true",
                "prometheus.io/port": str(VLLM_PORT),
                "prometheus.io/path": "/metrics",
            },
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": params.deployment_name},
            "ports": [
                {"name": "http", "port": SERVICE_PORT, "targetPort": VLLM_PORT}
            ],
        },
    }


def build_pdb(params: GpuParams) -> dict[str, Any]:
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": params.deployment_name, "namespace": params.namespace},
        "spec": {
            "minAvailable": 1,
            "selector": {"matchLabels": {"app": params.deployment_name}},
        },
    }


def build_network_policy(params: GpuParams) -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": params.network_policy_name, "namespace": params.namespace},
        "spec": {
            "podSelector": {"matchLabels": {"daalu.io/tier": "local"}},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}],
                    "ports": [{"port": 443}, {"port": 80}, {"port": VLLM_PORT}],
                },
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kube-system"
                                }
                            }
                        }
                    ],
                    "ports": [
                        {"port": 53, "protocol": "UDP"},
                        {"port": 53, "protocol": "TCP"},
                    ],
                },
                {
                    "to": [
                        {
                            "ipBlock": {
                                "cidr": "0.0.0.0/0",
                                "except": [
                                    "10.0.0.0/8",
                                    "172.16.0.0/12",
                                    "192.168.0.0/16",
                                    "169.254.0.0/16",
                                ],
                            }
                        }
                    ],
                    "ports": [{"port": 443}],
                },
            ],
        },
    }


def build_all(params: GpuParams, *, include_shared: bool = True) -> list[dict[str, Any]]:
    """Full ordered manifest bundle for one vLLM stack.

    Order matters: namespace and the HF Secret must exist before the
    Deployment references them.

    ``include_shared=False`` omits the Namespace and the ``hf-token``
    Secret (namespace singletons) and the NetworkPolicy — use it for a
    *second* stack co-resident in a namespace the first already
    provisioned (e.g. the coder alongside the classifier), so the two
    bundles don't fight over those shared, name-fixed objects.
    """
    bundle: list[dict[str, Any]] = []
    if include_shared:
        bundle += [build_namespace(params), build_hf_token_secret(params)]
    bundle += [
        build_vllm_deployment(params),
        build_service(params),
        build_pdb(params),
    ]
    if include_shared:
        bundle.append(build_network_policy(params))
    return bundle


# ── Classifier stack (catalog-driven) ────────────────────────────────────


def classifier_params(
    served_model_name: str,
    *,
    gpu_memory_utilization: float | None = None,
    **overrides: Any,
) -> GpuParams:
    """Build ``GpuParams`` for the tenant's SRE-agent model.

    Looks ``served_model_name`` up in ``model_catalog.classifier``. An
    unknown name keeps the legacy defaults (the Llama-3.1-8B stack), so rows
    written before the catalog render exactly as before. A non-NULL
    ``gpu_memory_utilization`` (``GpuTenant.gpu_memory_utilization``) beats
    the catalog value — that is how a time-sliced card fits two vLLMs.
    """
    from daalu_automation.core import model_catalog  # pure-data module

    spec = model_catalog.classifier(served_model_name)
    params = GpuParams(served_model_name=served_model_name, **overrides)
    if spec is not None:
        params.hf_model = spec.hf_model
        params.quantization = spec.quantization
        params.max_model_len = spec.context_len
        params.tool_call_parser = spec.tool_call_parser
        params.gpu_memory_utilization = spec.gpu_memory_utilization
        params.extra_args = spec.extra_args
        params.mem_request = spec.mem_request
        params.mem_limit = spec.mem_limit
    if gpu_memory_utilization is not None:
        params.gpu_memory_utilization = gpu_memory_utilization
    return params


# ── Coding-model stack (catalog-driven) ──────────────────────────────────


def coder_params_from_catalog(
    model: Any,
    *,
    namespace: str = "daalu",
    hf_token: str = "",
) -> GpuParams:
    """Build ``GpuParams`` for a coding model from a ``CodingModel`` entry.

    ``model`` is a ``core.model_catalog.CodingModel``. The coder runs as a
    distinctly-named stack (``qwen-coder`` / ``llm-coder``) so it can sit
    next to the classifier without colliding. The served-model-name,
    HF repo, quantization, context length and target GPU class all come
    from the catalog so the five-way name contract holds by construction.
    """
    # A 32B/30B checkpoint pulls a larger file into host page cache than the
    # 14B, so the 48 GB-class coders bump the container memory envelope.
    big = model.gpu_class == "ada-48"
    return GpuParams(
        namespace=namespace,
        gpu_class=model.gpu_class,
        deployment_name="qwen-coder",
        service_name="llm-coder",
        network_policy_name="coder-llm-egress",
        hf_token=hf_token,
        served_model_name=model.served_model_name,
        hf_model=model.hf_model,
        quantization=model.quantization,
        max_model_len=model.context_len,
        tool_call_parser=model.tool_call_parser,
        tenant_label="operator-coder",
        mem_request="16Gi" if big else "12Gi",
        mem_limit="32Gi" if big else "20Gi",
    )
