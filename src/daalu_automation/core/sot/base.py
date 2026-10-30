"""Source-of-Truth abstract base.

Every method takes ``db: AsyncSession`` and ``tenant_id`` explicitly so
each implementation can resolve its credentials per-tenant via the
``Integration`` row — mirrors the ``get_tenant_config`` shape used
everywhere else in this codebase.
"""

from __future__ import annotations

import abc
import uuid
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.core.sot.models import (
    Device,
    IntendedConfig,
    ObservedSnapshot,
    SoTRevision,
)


class SourceOfTruth(abc.ABC):
    """Read/write surface over a customer's SoT (Nautobot / NetBox / …)."""

    provider: ClassVar[str]

    @abc.abstractmethod
    async def list_devices(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        *,
        platform: str | None = None,
    ) -> list[Device]: ...

    @abc.abstractmethod
    async def get_device(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        device_id: str,
    ) -> Device | None: ...

    @abc.abstractmethod
    async def get_intended_config(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        device_id: str,
    ) -> IntendedConfig | None: ...

    @abc.abstractmethod
    async def put_intended_config(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        device_id: str,
        intended: IntendedConfig,
    ) -> SoTRevision: ...

    @abc.abstractmethod
    async def record_observed(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        observed: ObservedSnapshot,
    ) -> None: ...

    @abc.abstractmethod
    async def health(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> tuple[bool, str]: ...
