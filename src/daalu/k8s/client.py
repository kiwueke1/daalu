# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/k8s/client.py
from __future__ import annotations

import time
from typing import Optional

# We keep this import optional so unit tests can run without the package.
try:
    from kubernetes import client, config
except Exception:  # pragma: no cover - optional dependency
    client = None
    config = None


def wait_for_rollout(namespace: str, selector: str, timeout_seconds: int = 300, kube_context: Optional[str] = None) -> None:
    """
    Naive waiter for Deployments matching a label selector.
    Waits until all deployments have availableReplicas == desired.

    Args:
        namespace: namespace to check
        selector: label selector, e.g. "app=keystone"
        timeout_seconds: max time to wait
        kube_context: optional kube context to load
    """
    if client is None or config is None:
        # No kubernetes package available; act as a no-op
        return

    # load config
    if kube_context:
        config.load_kube_config(context=kube_context)
    else:
        config.load_kube_config()

    api = client.AppsV1Api()

    end = time.time() + timeout_seconds
    while time.time() < end:
        ready = True
        resp = api.list_namespaced_deployment(namespace=namespace, label_selector=selector)
        for d in resp.items:
            desired = d.spec.replicas or 0
            available = (d.status.available_replicas or 0)
            if available < desired:
                ready = False
                break
        if ready:
            return
        time.sleep(2)

    raise TimeoutError(f"Timeout waiting for rollout: ns={namespace} selector={selector}")
