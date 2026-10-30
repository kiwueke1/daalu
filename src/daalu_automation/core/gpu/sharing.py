"""Provider grant + shared-pool registration (doc 13).

Thin service helpers the admin/onboarding layer calls:

* :func:`grant_provider` — superuser flips ``Tenant.is_gpu_provider``. The
  schema allows N providers; policy grants exactly one today (the operator).
* :func:`register_pool` — once a provider's GpuTenant is ``active`` and
  ``shared``, point the inference-gateway at it by upserting a ``gpu_pools``
  row. Re-validates the provider invariant here too (defence in depth on top
  of the gpu-controller guard and the DB trigger).

Kept out of the gpu-controller process so daalu-api can call them directly
without a service-token round-trip.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.models.gpu_pool import GpuPool
from daalu_automation.models.gpu_tenant import GpuTenant
from daalu_automation.models.tenant import Tenant


class SharingError(RuntimeError):
    """A sharing invariant was violated (non-provider tried to share)."""


async def grant_provider(
    db: AsyncSession, tenant_id: uuid.UUID, *, enabled: bool = True
) -> Tenant:
    """Grant/revoke the GPU-provider capability. Superuser-gated by the caller."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise SharingError(f"tenant {tenant_id} not found")
    tenant.is_gpu_provider = enabled
    await db.commit()
    await db.refresh(tenant)
    return tenant


async def register_pool(
    db: AsyncSession,
    *,
    gpu_tenant: GpuTenant,
    upstream_url: str,
    served_models: list[Any] | None = None,
    capacity_hint: str | None = None,
) -> GpuPool:
    """Upsert the ``gpu_pools`` row backing a provider's shared GpuTenant.

    Raises :class:`SharingError` unless the GpuTenant is ``shared`` and its
    owner holds ``is_gpu_provider`` — the same invariant the gpu-controller
    and the DB trigger enforce, checked once more at the registration seam.
    """
    if not gpu_tenant.shared:
        raise SharingError("gpu_tenant is not marked shared")
    owner = await db.get(Tenant, gpu_tenant.tenant_id)
    if owner is None or not owner.is_gpu_provider:
        raise SharingError(
            f"tenant {gpu_tenant.tenant_id} is not a granted GPU provider"
        )

    existing = (
        await db.execute(
            select(GpuPool).where(GpuPool.gpu_tenant_id == gpu_tenant.id)
        )
    ).scalar_one_or_none()

    if existing is None:
        pool = GpuPool(
            provider_tenant_id=gpu_tenant.tenant_id,
            gpu_tenant_id=gpu_tenant.id,
            upstream_url=upstream_url,
            served_models=served_models or [],
            gpu_class=gpu_tenant.gpu_class,
            enabled=True,
            capacity_hint=capacity_hint,
        )
        db.add(pool)
    else:
        existing.provider_tenant_id = gpu_tenant.tenant_id
        existing.upstream_url = upstream_url
        existing.served_models = served_models or existing.served_models
        existing.gpu_class = gpu_tenant.gpu_class
        existing.enabled = True
        if capacity_hint is not None:
            existing.capacity_hint = capacity_hint
        pool = existing

    await db.commit()
    await db.refresh(pool)
    return pool
