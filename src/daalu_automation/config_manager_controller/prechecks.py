"""Tier-A ``host_cluster_ready`` precheck for the config-manager-controller.

Before the reconcile loop runs ``helm upgrade --install`` of the NV-CM
chart, confirm the target cluster has the cluster-scoped singletons the
chart's CRs depend on (engineer chapter 64 §64.2 step 1):

* **Envoy Gateway** — the shared ``GatewayClass`` (``envoy-gateway``) the
  chart references with ``createGatewayClass=false``.
* **cert-manager** — its ``Certificate`` + ``ClusterIssuer`` CRDs, so
  ``*.<slug>.cm.daalu.io`` certs can issue.
* **CloudNativePG** — its ``Cluster`` CRD, the chart's Postgres backend.

Without these, ``helm install`` either fails with an opaque "no matches
for kind" error or silently leaves Gateways/Certificates stuck Pending.
Catching it here flips the row to ``error`` with a precise "missing X"
message instead.

The chart-creation logic itself runs as the controller's ServiceAccount
in host-cluster mode (or the tunnel kubeconfig in customer-cluster mode);
this precheck reads against the same target.
"""

from __future__ import annotations

from typing import Any

import structlog
from kubernetes_asyncio import client as k8s_client

from daalu_automation.config_manager_controller.values import SHARED_GATEWAY_CLASS
from daalu_automation.nautobot_controller.k8s import (
    load_kubeconfig_dict,
    load_local_config,
)

logger = structlog.get_logger(__name__)

# CRD name → human label used in the "missing …" error. These are the
# Tier-A operators' CRDs; the operators themselves are installed once,
# out-of-band (§64.2 step 1).
REQUIRED_CRDS: dict[str, str] = {
    "gatewayclasses.gateway.networking.k8s.io": "Envoy Gateway (Gateway API CRDs)",
    "certificates.cert-manager.io": "cert-manager",
    "clusterissuers.cert-manager.io": "cert-manager ClusterIssuer support",
    "clusters.postgresql.cnpg.io": "CloudNativePG operator",
}


def evaluate_readiness(
    *, present_crds: set[str], gatewayclass_present: bool
) -> tuple[bool, list[str]]:
    """Pure decision step: given what's installed, what (if anything) is missing.

    Split out from the I/O so it can be unit-tested without a cluster.
    Returns ``(ready, missing_labels)``.
    """
    missing: list[str] = []
    for crd_name, label in REQUIRED_CRDS.items():
        if crd_name not in present_crds:
            missing.append(label)
    # The GatewayClass is an *object*, not just a CRD: the chart references
    # the shared class by name, so its mere CRD presence isn't enough.
    if "gatewayclasses.gateway.networking.k8s.io" in present_crds and not gatewayclass_present:
        missing.append(f"shared GatewayClass '{SHARED_GATEWAY_CLASS}'")
    return (not missing), missing


async def _gatewayclass_exists(api: k8s_client.ApiClient, name: str) -> bool:
    """True if the cluster-scoped GatewayClass ``name`` exists (any version)."""
    co = k8s_client.CustomObjectsApi(api)
    for version in ("v1", "v1beta1"):
        try:
            await co.get_cluster_custom_object(
                group="gateway.networking.k8s.io",
                version=version,
                plural="gatewayclasses",
                name=name,
            )
            return True
        except k8s_client.exceptions.ApiException as e:
            if e.status == 404:
                # 404 can mean "no such object" OR "no such version" — try
                # the next version before concluding it's absent.
                continue
            raise
    return False


async def host_cluster_ready(
    kubeconfig: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Probe the target cluster for the Tier-A singletons.

    ``kubeconfig is None`` → in-cluster/host mode (controller's SA).
    Otherwise → the customer cluster reached over the tunnel.
    Returns ``(ready, missing_labels)``; on a probe error returns
    ``(False, ["host-cluster precheck failed: …"])`` so the caller treats
    an unreachable cluster as not-ready rather than crashing the loop.
    """
    if kubeconfig is None:
        await load_local_config()
    else:
        await load_kubeconfig_dict(kubeconfig)

    api = k8s_client.ApiClient()
    try:
        ext = k8s_client.ApiextensionsV1Api(api)
        present: set[str] = set()
        for crd_name in REQUIRED_CRDS:
            try:
                await ext.read_custom_resource_definition(name=crd_name)
                present.add(crd_name)
            except k8s_client.exceptions.ApiException as e:
                if e.status == 404:
                    continue
                raise
        gc_present = False
        if "gatewayclasses.gateway.networking.k8s.io" in present:
            gc_present = await _gatewayclass_exists(api, SHARED_GATEWAY_CLASS)
        return evaluate_readiness(
            present_crds=present, gatewayclass_present=gc_present
        )
    except Exception as e:  # noqa: BLE001 — surface as not-ready, never crash
        logger.warning("config_manager_controller.host_precheck_error", error=str(e))
        return False, [f"host-cluster precheck failed: {type(e).__name__}: {e}"]
    finally:
        await api.close()
