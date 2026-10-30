"""Multi-tenant GPU-sharing model (doc 13).

Proves the load-bearing properties:

* only a granted provider may share a card (``register_pool`` refuses a
  non-provider — the same invariant the gpu-controller 403 and the DB
  trigger enforce);
* the gateway resolves the right shared pool (or falls back) for a request;
* each shared-pool call credits the GPU owner in the revenue-share ledger,
  and ``provider_earnings`` aggregates it.

Pure-function tests (credit math, engine params) run without a DB; the rest
use the shared ``db``/``seeded`` fixtures and auto-skip when Postgres is
unreachable.
"""

from __future__ import annotations

from daalu_automation.core.billing import provider_earnings
from daalu_automation.core.gpu.sharing import (
    SharingError,
    grant_provider,
    register_pool,
)
from daalu_automation.gpu_controller import manifests
from daalu_automation.inference_gateway.metering import (
    provider_credit,
    record_revenue_share,
)
from daalu_automation.inference_gateway.pool import resolve_upstream
from daalu_automation.models.gpu_pool import GpuPool
from daalu_automation.models.gpu_tenant import GpuTenant, GpuTenantState

# ── pure functions (no DB) ────────────────────────────────────────────────


def test_provider_credit_take_rate():
    assert provider_credit(1.0, 0.0) == 1.0
    assert provider_credit(1.0, 0.3) == 0.7
    # clamps out-of-range take rates
    assert provider_credit(1.0, 1.5) == 0.0
    assert provider_credit(1.0, -1.0) == 1.0


def test_engine_selection_reflected_in_manifest():
    # Default = vLLM + AWQ on the 16 GB Ada.
    default = manifests.build_vllm_deployment(manifests.GpuParams())
    args = default["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--quantization=awq_marlin" in args
    assert default["metadata"]["labels"]["daalu.io/engine"] == "vllm"
    assert "schedulerName" not in default["spec"]["template"]["spec"]

    # FP8 (no --quantization), a perf-tier engine label, KAI scheduler + queue.
    p = manifests.GpuParams(
        engine="tensorrt_llm",
        quantization="",
        scheduler_name="kai-scheduler",
        tenant_label="tenant-x",
        extra_args=("--max-num-seqs=256",),
    )
    dep = manifests.build_vllm_deployment(p)
    spec = dep["spec"]["template"]["spec"]
    cargs = spec["containers"][0]["args"]
    assert not any(a.startswith("--quantization") for a in cargs)
    assert "--max-num-seqs=256" in cargs
    assert spec["schedulerName"] == "kai-scheduler"
    assert dep["metadata"]["labels"]["daalu.io/engine"] == "tensorrt_llm"
    assert dep["spec"]["template"]["metadata"]["labels"]["tenant"] == "tenant-x"


# ── service-layer guard + pool registry (DB) ──────────────────────────────


async def _make_gpu_tenant(db, tenant_id, *, shared: bool) -> GpuTenant:
    row = GpuTenant(
        tenant_id=tenant_id,
        state=GpuTenantState.active,
        gpu_class="ada-48",
        model_classifier="meta/llama-3.1-8b-instruct",
        shared=shared,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def test_register_pool_requires_provider_grant(db, seeded):
    gt = await _make_gpu_tenant(db, seeded.tenant_a_id, shared=True)

    # Not a provider yet -> refused.
    try:
        await register_pool(db, gpu_tenant=gt, upstream_url="http://10.200.0.2:8888")
        raised = False
    except SharingError:
        raised = True
    assert raised, "non-provider must not be able to register a shared pool"

    # Grant, then it works.
    await grant_provider(db, seeded.tenant_a_id)
    pool = await register_pool(
        db,
        gpu_tenant=gt,
        upstream_url="http://10.200.0.2:8888",
        served_models=["meta/llama-3.1-8b-instruct"],
    )
    assert pool.provider_tenant_id == seeded.tenant_a_id
    assert pool.enabled is True
    assert pool.upstream_url == "http://10.200.0.2:8888"


async def test_register_pool_refuses_unshared_stack(db, seeded):
    await grant_provider(db, seeded.tenant_a_id)
    gt = await _make_gpu_tenant(db, seeded.tenant_a_id, shared=False)
    try:
        await register_pool(db, gpu_tenant=gt, upstream_url="http://x:8888")
        raised = False
    except SharingError:
        raised = True
    assert raised, "a stack not marked shared cannot back a pool"


async def test_resolve_upstream_selection_and_fallback(db, seeded):
    # No pools -> env fallback (empty by default), no provider to credit.
    r0 = await resolve_upstream(db, "meta/llama-3.1-8b-instruct")
    assert r0.provider_tenant_id is None
    assert r0.pool_id is None

    # A pool serving any model is selected and names its provider.
    db.add(
        GpuPool(
            provider_tenant_id=seeded.tenant_a_id,
            upstream_url="http://10.200.0.2:8888",
            served_models=[],
            gpu_class="ada-48",
            enabled=True,
        )
    )
    await db.commit()
    r1 = await resolve_upstream(db, "meta/llama-3.1-8b-instruct")
    assert r1.provider_tenant_id == str(seeded.tenant_a_id)
    assert r1.upstream_url == "http://10.200.0.2:8888"


async def test_model_specific_pool_does_not_match_other_model(db, seeded):
    db.add(
        GpuPool(
            provider_tenant_id=seeded.tenant_a_id,
            upstream_url="http://10.200.0.2:8888",
            served_models=["some/other-model"],
            gpu_class="ada-48",
            enabled=True,
        )
    )
    await db.commit()
    # Requested model isn't served by the only pool -> falls back.
    r = await resolve_upstream(db, "meta/llama-3.1-8b-instruct")
    assert r.provider_tenant_id is None


async def test_revenue_share_credits_provider_and_aggregates(db, seeded):
    # tenant B consumes tenant A's shared card.
    await record_revenue_share(
        db,
        consumer_tenant_id=str(seeded.tenant_b_id),
        provider_tenant_id=str(seeded.tenant_a_id),
        pool_id=None,
        prompt_tokens=1000,
        completion_tokens=200,
        gross_usd=0.0003,
    )
    earnings = await provider_earnings(db, seeded.tenant_a_id)
    assert earnings.calls == 1
    assert earnings.prompt_tokens == 1000
    assert earnings.completion_tokens == 200
    # default platform_take_rate is 0 -> credit equals gross
    assert round(earnings.gross_usd, 6) == 0.0003
    assert round(earnings.credit_usd, 6) == 0.0003
    # the consuming tenant earns nothing as a provider
    assert (await provider_earnings(db, seeded.tenant_b_id)).calls == 0
