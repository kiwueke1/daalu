"""Resolve a tenant's role in the AI factory, for the native GPU UI.

A tenant relates to GPUs in one of a few ways, and each sees different things:

* **owner**    — has its own GpuTenant (SOVEREIGN, not shared): sees that card's
                 hardware metrics (temp/util/VRAM/power/health).
* **provider** — granted ``is_gpu_provider`` and runs a shared ``gpu_pools`` row:
                 sees the shared card's hardware + (later) its consumers.
* **consumer** — enrolled in the daalu-hosted tier (``daalu_hosted_quotas``):
                 sees *usage*-centric metrics (their tokens/quota/latency), NOT
                 the raw hardware health of someone else's card.
* **none**     — no GPU relationship.

The metric scope is a tenant-pinned label selector; per-tenant attribution
works because the gpu-controller stamps the vLLM pod's ``tenant`` label with the
owner's tenant id (see ``gpu_controller``), which dcgm-exporter + the
``nvidia-dcgm`` ServiceMonitor relabeling carry onto the DCGM series.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.models.daalu_hosted_quota import DaaluHostedQuota
from daalu_automation.models.gpu_pool import GpuPool
from daalu_automation.models.gpu_tenant import GpuTenant, GpuTenantState
from daalu_automation.models.tenant import Tenant


@dataclass(slots=True)
class FactoryView:
    role: str  # "owner" | "provider" | "consumer" | "none"
    is_owner: bool = False
    is_provider: bool = False
    is_consumer: bool = False
    has_gpu: bool = False  # owns/provides a physical card the UI shows hardware for
    gpu_class: str | None = None
    # The tenant value to pin DCGM series to (owner/provider hardware scope).
    tenant_label: str | None = None
    panels: list[str] = field(default_factory=list)


async def resolve_factory_view(db: AsyncSession, tenant_id: uuid.UUID) -> FactoryView:
    tenant = await db.get(Tenant, tenant_id)

    gpu = (
        await db.execute(select(GpuTenant).where(GpuTenant.tenant_id == tenant_id))
    ).scalar_one_or_none()
    # Disconnect keeps the row as ``destroyed`` (one row per tenant), so a
    # disconnected GPU must not keep the tenant an "owner": the card, its
    # class and the hardware panels would stay up after removing it. The
    # ~30 s ``deleting`` teardown counts as gone too — the user already
    # pressed Disconnect.
    if gpu is not None and gpu.state in (GpuTenantState.deleting, GpuTenantState.destroyed):
        gpu = None
    pool = (
        await db.execute(
            select(GpuPool).where(GpuPool.provider_tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    quota = (
        await db.execute(
            select(DaaluHostedQuota).where(DaaluHostedQuota.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()

    is_provider = bool(tenant and tenant.is_gpu_provider and pool is not None)
    # An owner has their own card that is NOT a shared pool (that's the provider).
    is_owner = gpu is not None and not gpu.shared and not is_provider
    is_consumer = quota is not None and quota.enabled

    if is_provider:
        role = "provider"
    elif is_owner:
        role = "owner"
    elif is_consumer:
        role = "consumer"
    else:
        role = "none"

    gpu_class = None
    if pool is not None:
        gpu_class = pool.gpu_class
    elif gpu is not None:
        gpu_class = gpu.gpu_class

    has_gpu = is_owner or is_provider
    panels: list[str] = []
    if has_gpu:
        panels = ["metrics", "events", "alerts", "diagnostics", "validate"]
        if is_provider:
            panels.append("consumers")
    if is_consumer:
        panels.append("consumer")

    return FactoryView(
        role=role,
        is_owner=is_owner,
        is_provider=is_provider,
        is_consumer=is_consumer,
        has_gpu=has_gpu,
        gpu_class=gpu_class,
        tenant_label=str(tenant_id) if has_gpu else None,
        panels=panels,
    )
