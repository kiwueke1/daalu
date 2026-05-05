"""Temporal client + per-namespace helpers.

We maintain one `Client` per namespace (Temporal's Python SDK pins the
namespace at connect time). Connections are lazily opened and cached
for the life of the process.
"""

from __future__ import annotations

import structlog
from temporalio.client import Client

from temporal_console.config import get_settings

logger = structlog.get_logger(__name__)

_clients: dict[str, Client] = {}


async def get_client(namespace: str) -> Client:
    """Return a connected Client for the given namespace, caching it."""
    if namespace in _clients:
        return _clients[namespace]
    settings = get_settings()
    logger.info("temporal.connect", namespace=namespace, host=settings.temporal_host)
    client = await Client.connect(settings.temporal_host, namespace=namespace)
    _clients[namespace] = client
    return client


async def close_all() -> None:
    """Clean up all cached clients on shutdown."""
    for ns, c in list(_clients.items()):
        try:
            # temporalio Client has no async close; drop the reference
            # and let the underlying service client GC.
            _clients.pop(ns, None)
        except Exception:
            logger.warning("temporal.close_failed", namespace=ns)
