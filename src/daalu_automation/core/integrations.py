"""Adapter registry for external systems.

An integration is a small object that knows how to:

- ``ingest(tenant_id)`` — pull recent state from the external system
  for one tenant and emit events scoped to that tenant,
- ``push()`` — execute an action in the external system on the
  platform's behalf,
- ``health()`` — quick check the credentials still work.

The registry pattern lets the Integrations page enumerate providers
dynamically — modules just register their adapters at import time.

Multi-tenancy. Every ``ingest()`` is per-tenant: the adapter reads that
tenant's connection details from ``get_tenant_config`` and stamps
emitted events with the same ``tenant_id``. The scheduler fans out one
task per tenant; ``POST /integrations/{provider}/ingest`` runs against
the caller's own tenant.
"""

from __future__ import annotations

import abc
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class IntegrationDescriptor:
    provider: str
    module: str
    display_name: str
    description: str
    # Which env keys the integration needs to be considered configured.
    # The Integrations page reads these to render a "Missing: XYZ" hint.
    required_settings: tuple[str, ...]


class IntegrationAdapter(abc.ABC):
    descriptor: IntegrationDescriptor

    @abc.abstractmethod
    async def ingest(self, tenant_id: uuid.UUID) -> int:  # events emitted
        ...

    async def push(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(f"{self.descriptor.provider} has no push() support yet")

    async def health(self, tenant_id: uuid.UUID) -> tuple[bool, str]:
        """Probe the external system for this tenant.

        Returns ``(ok, message)``. ``ok=False`` flips the matching
        ``Integration`` row's status to ``error`` and the message is
        surfaced as ``last_error`` for the UI.

        The default returns ok — adapters without a meaningful probe
        (e.g. local-only integrations, write-only sinks) can leave it
        alone and their UI badge stays green until something else
        flips it. Adapters that talk to a remote system should override
        with a real probe (e.g. ``GET /-/healthy`` for Prometheus).

        Called by the ``integrations.health_check`` beat task every
        tick — keep the probe under a few seconds.
        """
        return True, "ok"


_REGISTRY: dict[str, Callable[[], IntegrationAdapter]] = {}


def register_integration(
    factory: Callable[[], IntegrationAdapter]
) -> Callable[[], IntegrationAdapter]:
    instance = factory()
    _REGISTRY[instance.descriptor.provider] = factory
    logger.info(
        "integration.registered",
        provider=instance.descriptor.provider,
        module=instance.descriptor.module,
    )
    return factory


def list_integrations() -> list[IntegrationDescriptor]:
    return [factory().descriptor for factory in _REGISTRY.values()]


def get_integration(provider: str) -> IntegrationAdapter:
    return _REGISTRY[provider]()
