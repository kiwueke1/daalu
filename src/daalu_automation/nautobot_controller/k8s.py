"""K8s materialisation for per-tenant Nautobot stacks.

Two modes:

* **In-cluster** (operator-cluster) — uses kubernetes_asyncio's
  in-cluster config from the ServiceAccount mounted into the
  controller pod. Targets the local hub cluster.
* **Kubeconfig** (customer-cluster) — loads a kubeconfig dict from
  the customer's ClusterTunnel→Integration row. Targets the
  customer's federated cluster via the wg tunnel.

Both modes use the same ``apply_one`` / ``delete_one`` helpers driven
off the manifest dicts from :mod:`nautobot_controller.manifests`.
Dispatch is generic across the GVKs we use, so the reconcile loop
doesn't have to special-case each resource kind.
"""

from __future__ import annotations

import tempfile
from typing import Any

import structlog
import yaml
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config

logger = structlog.get_logger(__name__)


# ── Client factory ─────────────────────────────────────────────────────


async def load_local_config() -> None:
    """Pick in-cluster vs kubeconfig once for operator-cluster mode.

    ``load_incluster_config()`` is *synchronous* in kubernetes_asyncio
    (it just reads the mounted SA token/CA) and returns None, so it must
    NOT be awaited — awaiting it raises ``TypeError: object NoneType
    can't be used in 'await'`` whenever this runs in-cluster. Only
    ``load_kube_config()`` is a coroutine.
    """
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        await k8s_config.load_kube_config()


async def load_kubeconfig_dict(kubeconfig: dict[str, Any]) -> None:
    """Load a kubeconfig dict (e.g. the cluster_tunnel's stored config).

    kubernetes_asyncio's ``load_kube_config_from_dict`` is available
    in 31+ but not earlier; for portability we write to a tempfile
    and call the file-based loader. The tempfile is closed
    immediately after, so kubeconfig credentials don't linger on
    disk.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kubeconfig", delete=True
    ) as fh:
        yaml.safe_dump(kubeconfig, fh)
        fh.flush()
        await k8s_config.load_kube_config(config_file=fh.name)


# ── Generic apply / delete ─────────────────────────────────────────────


_RESOURCE_PLURALS: dict[tuple[str, str], tuple[str, str]] = {
    # (apiVersion, kind) → (api_class_name, method_suffix)
    ("v1", "Namespace"): ("CoreV1Api", "namespace"),
    ("v1", "Secret"): ("CoreV1Api", "namespaced_secret"),
    ("v1", "ConfigMap"): ("CoreV1Api", "namespaced_config_map"),
    ("v1", "Service"): ("CoreV1Api", "namespaced_service"),
    ("v1", "PersistentVolumeClaim"): (
        "CoreV1Api",
        "namespaced_persistent_volume_claim",
    ),
    ("apps/v1", "Deployment"): ("AppsV1Api", "namespaced_deployment"),
    ("apps/v1", "StatefulSet"): ("AppsV1Api", "namespaced_stateful_set"),
    ("policy/v1", "PodDisruptionBudget"): (
        "PolicyV1Api",
        "namespaced_pod_disruption_budget",
    ),
    ("networking.k8s.io/v1", "Ingress"): (
        "NetworkingV1Api",
        "namespaced_ingress",
    ),
    ("networking.k8s.io/v1", "NetworkPolicy"): (
        "NetworkingV1Api",
        "namespaced_network_policy",
    ),
}


def _resolve(manifest: dict[str, Any]) -> tuple[str, str, bool]:
    """Map a manifest to (api_class_name, method_suffix, is_namespaced).

    Raises if the GVK isn't in the table — callers should only feed
    manifests from :mod:`.manifests`, so an unknown GVK indicates a
    code-path that bypassed the registry.
    """
    key = (manifest["apiVersion"], manifest["kind"])
    if key not in _RESOURCE_PLURALS:
        raise ValueError(f"no k8s dispatch entry for {key}")
    api_cls, suffix = _RESOURCE_PLURALS[key]
    is_ns = key != ("v1", "Namespace")
    return api_cls, suffix, is_ns


async def apply_one(api: k8s_client.ApiClient, manifest: dict[str, Any]) -> None:
    """Create-or-patch one manifest. Idempotent.

    Uses the kubernetes_asyncio dynamic dispatch pattern: look up the
    API class on the client, then call ``create_…`` and on 409 fall
    back to ``patch_…``. We don't use server-side apply (kubectl
    apply style) because kubernetes_asyncio's SSA support is uneven
    across versions.
    """
    api_cls_name, suffix, is_namespaced = _resolve(manifest)
    api_cls = getattr(k8s_client, api_cls_name)
    api_inst = api_cls(api)
    name = manifest["metadata"]["name"]
    namespace = manifest["metadata"].get("namespace")
    create_method = getattr(api_inst, f"create_{suffix}")
    patch_method = getattr(api_inst, f"patch_{suffix}")
    try:
        if is_namespaced:
            await create_method(namespace=namespace, body=manifest)
        else:
            await create_method(body=manifest)
        return
    except k8s_client.exceptions.ApiException as e:
        if e.status != 409:
            raise
    # Already exists — patch in place. JSON merge-patch over the same
    # body. Server-side validation rejects type-changing patches with
    # a clear message, so we don't try to be clever about diffs.
    try:
        if is_namespaced:
            await patch_method(name=name, namespace=namespace, body=manifest)
        else:
            await patch_method(name=name, body=manifest)
    except k8s_client.exceptions.ApiException:
        # Some fields (e.g. PVC.spec.resources) are immutable; a patch
        # would 422. Log and continue — the existing object's spec is
        # close enough, and a recreate dance would lose the user's data.
        logger.warning(
            "nautobot_controller.apply_one.patch_failed_immutable",
            kind=manifest["kind"],
            name=name,
            namespace=namespace,
        )


async def delete_one(api: k8s_client.ApiClient, manifest: dict[str, Any]) -> None:
    """Delete one manifest by name. 404 is fine.

    Used in the destroy path. PVCs aren't deleted by namespace-delete
    if reclaimPolicy is Retain — we delete them explicitly in
    ``destroy_tenant_stack`` only when the caller asks for a hard
    teardown.
    """
    api_cls_name, suffix, is_namespaced = _resolve(manifest)
    api_cls = getattr(k8s_client, api_cls_name)
    api_inst = api_cls(api)
    name = manifest["metadata"]["name"]
    namespace = manifest["metadata"].get("namespace")
    delete_method = getattr(api_inst, f"delete_{suffix}")
    try:
        if is_namespaced:
            await delete_method(name=name, namespace=namespace)
        else:
            await delete_method(name=name)
    except k8s_client.exceptions.ApiException as e:
        if e.status != 404:
            raise


# ── Readiness probes ───────────────────────────────────────────────────


async def web_deployment_ready(
    api: k8s_client.ApiClient, namespace: str
) -> bool:
    """Has the per-tenant Nautobot web Deployment reported a ready replica?

    Used by the reconcile loop's provisioning → active transition.
    The web Deployment runs a `migrate` init container; first-boot
    readiness takes ~30-90s.
    """
    apps = k8s_client.AppsV1Api(api)
    try:
        dep = await apps.read_namespaced_deployment(
            name="nautobot", namespace=namespace,
        )
    except k8s_client.exceptions.ApiException as e:
        if e.status == 404:
            return False
        raise
    status = getattr(dep, "status", None) or None
    if status is None:
        return False
    return int(getattr(status, "ready_replicas", 0) or 0) >= 1
