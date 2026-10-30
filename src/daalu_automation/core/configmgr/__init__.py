"""Thin async clients for a per-tenant NVIDIA Config Manager (NV-CM) stack.

We deploy NV-CM unmodified (vendored pinned chart) and talk to its REST
API over the WireGuard tunnel via its ``svc-*`` (JWT-only) endpoints.
These clients are written against the committed OpenAPI specs at
``deploy/charts/nv-config-manager-<ver>-api-specs/`` and authenticate
with a Keycloak client-credentials JWT (see ``core/keycloak``).

See docs/design/nv-config-manager-integration.md §8, §9.2, §13.
"""

from __future__ import annotations

from daalu_automation.core.configmgr.client import (
    ConfigStoreClient,
    NvcmClientError,
    NvcmConn,
    RenderClient,
    TemporalWorkflowClient,
    conn_from_integration_config,
)

__all__ = [
    "ConfigStoreClient",
    "NvcmClientError",
    "NvcmConn",
    "RenderClient",
    "TemporalWorkflowClient",
    "conn_from_integration_config",
]
