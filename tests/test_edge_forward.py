"""Unit tests for the hub→edge forwarder's path matcher.

The matcher decides, per request, whether to short-circuit to the
customer's edge or fall through to local handling. Getting this
wrong has security consequences either way:

* False positive (forwards a platform-scoped path) → confusing 502
  for the user when the edge doesn't implement that route.
* False negative (fails to forward a tenant-scoped path) → tenant
  data lands in the hub DB for a Private tenant. Sovereignty
  broken.

These tests enumerate every documented prefix and a few near-miss
cases.
"""

from __future__ import annotations

import pytest

from daalu_automation.core.edge_forward import (
    PLATFORM_SCOPED_PREFIXES,
    TENANT_SCOPED_PREFIXES,
    is_tenant_scoped,
)


@pytest.mark.parametrize("prefix", TENANT_SCOPED_PREFIXES)
def test_tenant_scoped_prefixes_match_themselves(prefix: str) -> None:
    """Every documented tenant prefix recognises ``/api/v1<prefix>``."""
    assert is_tenant_scoped(f"/api/v1{prefix}")


@pytest.mark.parametrize("prefix", TENANT_SCOPED_PREFIXES)
def test_tenant_scoped_prefixes_match_subpaths(prefix: str) -> None:
    """And ``/api/v1<prefix>/<anything>``."""
    assert is_tenant_scoped(f"/api/v1{prefix}/123/items")


@pytest.mark.parametrize("prefix", PLATFORM_SCOPED_PREFIXES)
def test_platform_scoped_prefixes_do_not_forward(prefix: str) -> None:
    """Auth, billing, tenants, etc. stay on the hub."""
    assert not is_tenant_scoped(f"/api/v1{prefix}")
    assert not is_tenant_scoped(f"/api/v1{prefix}/something")


def test_non_api_paths_do_not_forward() -> None:
    """Static / health / ingress paths never forward."""
    assert not is_tenant_scoped("/health")
    assert not is_tenant_scoped("/")
    assert not is_tenant_scoped("/metrics")
    assert not is_tenant_scoped("/version")
    assert not is_tenant_scoped("/ide/foo/bar")  # IDE proxy, not API


def test_near_miss_prefix_does_not_match() -> None:
    """``/api/v1/agents-foo`` must NOT match ``/agents``.

    The matcher uses exact-segment matching (path == prefix OR
    starts with prefix + '/'). A spuriously-named future router
    can't accidentally inherit tenant-scoped routing.
    """
    assert is_tenant_scoped("/api/v1/agents")
    assert is_tenant_scoped("/api/v1/agents/")
    assert is_tenant_scoped("/api/v1/agents/foo")
    assert not is_tenant_scoped("/api/v1/agents-foo")
    assert not is_tenant_scoped("/api/v1/agentstuff")


def test_custom_api_prefix() -> None:
    """The matcher honours the configured ``api_v1_prefix``."""
    assert is_tenant_scoped("/v2/alerts", api_prefix="/v2")
    assert not is_tenant_scoped("/api/v1/alerts", api_prefix="/v2")


def test_internal_routes_do_not_forward() -> None:
    """The hub-side internal API (edge-agents tasks/results) stays on the hub."""
    assert not is_tenant_scoped("/api/v1/internal/agent-tasks")
    assert not is_tenant_scoped("/api/v1/internal/agent-results")


def test_workspaces_ide_sessions_forwards() -> None:
    """The workspace IDE-session endpoint is tenant-scoped (the
    workspaces table holds the row)."""
    assert is_tenant_scoped("/api/v1/ide/sessions")
    assert is_tenant_scoped("/api/v1/ide/sessions/me")
