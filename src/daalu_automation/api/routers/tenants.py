"""Tenant management — superuser-only.

A platform superuser uses these endpoints to onboard a new customer
(POST /tenants), rotate that customer's ingest key, and soft-delete
defunct tenants. No tenant-scoped user can reach this router; even a
tenant-admin from one customer cannot list or read other tenants.

The ingest API key is **returned in cleartext exactly once** — at
create time and after rotation. The DB only persists its sha256 hash;
once the response is delivered the cleartext is unrecoverable, and the
operator must store it in their secret manager. This mirrors how every
SaaS handles API keys (Stripe, GitHub, AWS) — losing the key means
rotating, not recovering.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_superuser
from daalu_automation.core.auth import generate_ingest_api_key, hash_ingest_api_key
from daalu_automation.database import get_db
from daalu_automation.models import Tenant, User  # noqa: F401 — User imported for Depends

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantCreate(BaseModel):
    name: str
    slug: str
    timezone: str = "UTC"


class TenantOut(BaseModel):
    id: str
    name: str
    slug: str
    timezone: str
    is_deleted: bool
    has_ingest_key: bool


class TenantCreatedOut(TenantOut):
    """Response returned exactly once on creation / key rotation.

    Includes the cleartext ``ingest_api_key`` — the only point in the
    lifecycle where it is visible to the operator.
    """

    ingest_api_key: str


def _to_out(t: Tenant) -> TenantOut:
    return TenantOut(
        id=str(t.id),
        name=t.name,
        slug=t.slug,
        timezone=t.timezone,
        is_deleted=t.is_deleted,
        has_ingest_key=t.ingest_api_key_hash is not None,
    )


@router.post("", response_model=TenantCreatedOut, status_code=201)
async def create_tenant(
    payload: TenantCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_superuser),
):
    existing = (
        await db.execute(select(Tenant).where(Tenant.slug == payload.slug))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"tenant slug already in use: {payload.slug}")

    key = generate_ingest_api_key()
    tenant = Tenant(
        name=payload.name,
        slug=payload.slug,
        timezone=payload.timezone,
        ingest_api_key_hash=hash_ingest_api_key(key),
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return TenantCreatedOut(**_to_out(tenant).model_dump(), ingest_api_key=key)


@router.get("", response_model=list[TenantOut])
async def list_tenants(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_superuser),
):
    rows = (
        await db.execute(select(Tenant).where(Tenant.is_deleted.is_(False)))
    ).scalars().all()
    return [_to_out(t) for t in rows]


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_superuser),
):
    try:
        tid = uuid.UUID(tenant_id)
    except ValueError as e:
        raise HTTPException(404, "tenant not found") from e
    tenant = await db.get(Tenant, tid)
    if tenant is None or tenant.is_deleted:
        raise HTTPException(404, "tenant not found")
    return _to_out(tenant)


@router.post("/{tenant_id}/rotate-ingest-key", response_model=TenantCreatedOut)
async def rotate_ingest_key(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_superuser),
):
    """Issue a fresh ingest key and invalidate the previous one.

    Returns the new cleartext key once — operator must update every
    shim's ``DAALU_INGEST_KEY`` Secret before the old key stops working
    (it stops working the moment this endpoint returns).
    """
    try:
        tid = uuid.UUID(tenant_id)
    except ValueError as e:
        raise HTTPException(404, "tenant not found") from e
    tenant = await db.get(Tenant, tid)
    if tenant is None or tenant.is_deleted:
        raise HTTPException(404, "tenant not found")
    key = generate_ingest_api_key()
    tenant.ingest_api_key_hash = hash_ingest_api_key(key)
    await db.commit()
    await db.refresh(tenant)
    return TenantCreatedOut(**_to_out(tenant).model_dump(), ingest_api_key=key)


@router.delete("/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_superuser),
):
    """Soft-delete a tenant.

    Sets ``is_deleted=True`` and clears the ingest key hash so any shim
    still pointed at this tenant fails closed. Data rows remain for
    audit / billing reconstruction.
    """
    try:
        tid = uuid.UUID(tenant_id)
    except ValueError as e:
        raise HTTPException(404, "tenant not found") from e
    tenant = await db.get(Tenant, tid)
    if tenant is None or tenant.is_deleted:
        raise HTTPException(404, "tenant not found")
    tenant.is_deleted = True
    tenant.ingest_api_key_hash = None
    await db.commit()
