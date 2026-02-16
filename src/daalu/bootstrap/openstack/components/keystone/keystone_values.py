# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/keystone/keystone_values.py

from __future__ import annotations

from typing import Any

from daalu.bootstrap.openstack.openstack_helm_endpoints import OpenStackHelmEndpoints


def build_keystone_values(
    *,
    endpoints_builder: OpenStackHelmEndpoints,
    kubectl,
    service: str = "keystone",
) -> dict[str, Any]:
    endpoints = endpoints_builder.build_common_endpoints(
        kubectl=kubectl,
        service=service,
        keystone_api_service="keystone-api",
    )

    # These are the scheduling bits you were trying to fix:
    scheduling = {
        "nodeSelector": {"openstack-control-plane": "enabled"},
        "pod": {
            "replicas": {"api": 1},
            # IMPORTANT: tolerations must be enabled AND match your taints
            "tolerations": {
                "keystone": {
                    "enabled": True,
                    "tolerations": [
                        {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"},
                        {"key": "node-role.kubernetes.io/master", "operator": "Exists", "effect": "NoSchedule"},
                    ],
                }
            },
        },
    }

    # Critical part: ensure endpoints.oslo_db.hosts.default is Percona HAProxy, not mariadb
    # (builder already does this). That fixes the dependency resolution for DB init jobs.
    #
    # Also make sure the chart is using endpoints.oslo_db for the dependency service name
    # (some charts hard-code "mariadb" dependency service label).
    #
    # If your keystone chart still *hardcodes* DEPENDENCY_SERVICE=mariadb,
    # you must override the chart's dependency settings if supported.

    values: dict[str, Any] = {
        "endpoints": endpoints,
        **scheduling,
    }

    return values
