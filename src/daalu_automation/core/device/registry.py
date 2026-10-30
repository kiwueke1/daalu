"""DeviceAdapter registry, keyed by ``transport``.

Mirrors the shape of :mod:`daalu_automation.core.integrations` but
keys by adapter ``transport`` (``"linux_ssh"``, future ``"redfish"``,
``"netconf"``, etc.) instead of provider slug — a transport can have
several vendor specializations layered on top.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

from daalu_automation.core.device.base import DeviceAdapter

logger = structlog.get_logger(__name__)


_REGISTRY: dict[str, Callable[[], DeviceAdapter]] = {}


def register_device_adapter(
    factory: Callable[[], DeviceAdapter],
) -> Callable[[], DeviceAdapter]:
    instance = factory()
    _REGISTRY[instance.transport] = factory
    logger.info("device_adapter.registered", transport=instance.transport)
    return factory


def get_device_adapter(transport: str) -> DeviceAdapter:
    return _REGISTRY[transport]()


def list_device_adapters() -> list[str]:
    return sorted(_REGISTRY.keys())
