"""Shared GPU pool registry — the upstreams the inference-gateway may serve from.

One row per *shared* GPU stack (a :class:`GpuTenant` flipped ``shared=True``
by a granted provider). The gateway selects an ``enabled`` pool that serves
the requested model and proxies to its ``upstream_url`` — replacing the
single static ``settings.daalu_hosted_upstream_url``.

Built multi-provider from day one (see
``docs/plans/nvidia-ai-factory/13-gpu-sharing-and-multi-tenant-marketplace.md``):
*N* providers may each register a pool, though policy grants
``Tenant.is_gpu_provider`` to exactly one tenant (the operator) today. The
``provider_tenant_id`` is who gets credited in ``gpu_revenue_share`` for
calls this pool serves.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from daalu_automation.database import Base
from daalu_automation.models._mixins import TimestampMixin, UUIDPrimaryKeyMixin


class GpuPool(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "gpu_pools"

    # The tenant that owns the physical card and is credited for usage.
    # Must hold ``Tenant.is_gpu_provider`` (enforced in the provision path
    # and by the gpu_tenants DB trigger that gates ``shared``).
    provider_tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The GpuTenant stack backing this pool (the shared vLLM). Nullable so a
    # pool can be pre-registered / manually pointed at an URL before the
    # controller row exists.
    gpu_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("gpu_tenants.id", ondelete="SET NULL"),
        nullable=True,
    )
    # OpenAI-compatible base URL the gateway proxies to. For a card on the
    # provider's own joined cluster this is the daalu-edge proxy URL over
    # the WireGuard tunnel (``http://<tunnel_ip>:8888``); for an in-hub card
    # it is the in-cluster Service URL.
    upstream_url: Mapped[str] = mapped_column(String(512), nullable=False)
    # Model ids this pool serves (``served-model-name`` values). The gateway
    # matches a request's model against this list; empty list = serves any.
    served_models: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    gpu_class: Mapped[str] = mapped_column(String(64), nullable=False, default="ada-48")
    # Disabled pools are skipped by the gateway (drain before teardown).
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Free-form capacity note for ops (e.g. measured tokens/sec from AIPerf).
    capacity_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
