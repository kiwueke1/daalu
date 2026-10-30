"""config-manager-controller — provisions per-tenant NV-CM stacks.

A hub-side reconciler (mirrors ``nautobot_controller``) that converges
each ``config_manager_tenants`` row to a running NVIDIA Config Manager
Helm release in the target cluster, over the WireGuard tunnel. Unlike the
nautobot-controller (which applies raw manifests), this one renders values
and runs ``helm upgrade --install`` of the vendored pinned chart.

See docs/design/nv-config-manager-integration.md §6.4.
"""

from __future__ import annotations

from daalu_automation.config_manager_controller.app import create_app

__all__ = ["create_app"]
