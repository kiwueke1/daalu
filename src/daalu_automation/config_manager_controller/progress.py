"""Live install progress for a tenant's NV-CM namespace.

An NV-CM first install is a long sequence of waits — image pulls, CNPG
bootstrap, Django migrations — during which the tenant row says nothing but
``provisioning``. Without pod-level detail the UI can only show a spinner, and
the two failure modes that actually happen (unschedulable pods, image pulls
that can't authenticate or verify TLS) are invisible until someone runs
kubectl.

This reads the tenant's namespace through whichever cluster the row targets
and summarises what is holding things up. Read-only.
"""

from __future__ import annotations

from typing import Any

import structlog
from kubernetes_asyncio import client as k8s_client

from daalu_automation.nautobot_controller.k8s import (
    load_kubeconfig_dict,
    load_local_config,
)

logger = structlog.get_logger(__name__)

# Pod-level reasons worth surfacing verbatim — these are the ones an operator
# can act on, as opposed to transient container churn.
_INTERESTING_EVENT_REASONS = {
    "FailedScheduling",
    "Failed",
    "FailedMount",
    "BackOff",
    "ErrImagePull",
    "ImagePullBackOff",
}


# Namespace Envoy Gateway provisions its proxy Services into, and the label it
# stamps them with. The proxy Service is named with a hash suffix
# (``envoy-<gw-ns>-<gw-name>-<hash>``) so it can't be constructed by hand —
# it has to be looked up by the owning-gateway label.
ENVOY_GATEWAY_NAMESPACE = "envoy-gateway-system"
_OWNING_GATEWAY_NS_LABEL = "gateway.envoyproxy.io/owning-gateway-namespace"


def _container_summary(pod: Any) -> tuple[int, int, str | None]:
    """Return ``(ready_count, total, blocking_reason)`` for a pod."""
    statuses = list(pod.status.container_statuses or [])
    inits = list(pod.status.init_container_statuses or [])
    ready = sum(1 for c in statuses if c.ready)
    total = len(statuses)

    # An init container that hasn't finished is what's actually blocking, so
    # report its reason rather than the main container's generic "waiting".
    for c in inits:
        w = getattr(c.state, "waiting", None)
        if w is not None and w.reason:
            return ready, total, f"Init:{w.reason}"
        t = getattr(c.state, "terminated", None)
        if t is not None and t.exit_code not in (0, None):
            return ready, total, f"Init:{t.reason or 'Error'}"
        if getattr(c.state, "running", None) is not None:
            return ready, total, "Init:Running"
    for c in statuses:
        w = getattr(c.state, "waiting", None)
        if w is not None and w.reason:
            return ready, total, w.reason
    return ready, total, None


async def gateway_internal_url(
    namespace: str, kubeconfig: dict[str, Any] | None
) -> str | None:
    """Return the in-cluster URL of the tenant gateway, or ``None``.

    This is the address the hub tool proxy dials (through the tenant's edge
    forward proxy over WireGuard) to reach the tenant's tool UIs. It is a
    cluster-internal Service address, not a public URL, which is why
    ``values.compute_urls`` can't derive it — it has to be read back from the
    cluster after Envoy Gateway has provisioned the proxy Service.

    Returned as the Service's ``*.svc.cluster.local`` name rather than its
    ClusterIP: the name survives a gateway re-provision, the IP does not. The
    edge proxy's default allowlist permits both.

    Never raises — a missing gateway is a normal mid-install state, and this
    feeds a status endpoint that must not 500.
    """
    try:
        if kubeconfig is None:
            await load_local_config()
        else:
            await load_kubeconfig_dict(kubeconfig)

        async with k8s_client.ApiClient() as api:
            core = k8s_client.CoreV1Api(api)
            svcs = await core.list_namespaced_service(
                ENVOY_GATEWAY_NAMESPACE,
                label_selector=f"{_OWNING_GATEWAY_NS_LABEL}={namespace}",
                timeout_seconds=10,
            )
        # One gateway per tenant namespace, so the first match is the one.
        for svc in svcs.items:
            ports = [p.port for p in (svc.spec.ports or []) if p.port]
            if not ports:
                continue
            host = f"{svc.metadata.name}.{ENVOY_GATEWAY_NAMESPACE}.svc.cluster.local"
            # HTTP, not HTTPS: the tenant gateway listens plaintext and the hub
            # terminates TLS at the public edge (see render_values).
            port = 80 if 80 in ports else ports[0]
            return f"http://{host}" if port == 80 else f"http://{host}:{port}"
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "config_manager_controller.gateway_lookup_failed",
            namespace=namespace,
            error=str(e),
        )
        return None


async def nautobot_api_token(
    namespace: str, kubeconfig: dict[str, Any] | None
) -> str | None:
    """Read the chart-generated Nautobot API token from the env namespace.

    The vendored Deployer creates a ``nautobot-token`` Secret (key ``token``)
    holding a superuser API token; it is the credential the hub's SoT client
    and bulk import authenticate with (closing the "populated by the
    controller in a follow-up step" gap noted in
    ``_upsert_config_manager_integration``). Never raises — a missing secret
    is a normal mid-install state.
    """
    import base64

    try:
        if kubeconfig is None:
            await load_local_config()
        else:
            await load_kubeconfig_dict(kubeconfig)

        async with k8s_client.ApiClient() as api:
            core = k8s_client.CoreV1Api(api)
            secret = await core.read_namespaced_secret("nautobot-token", namespace)
        raw = (secret.data or {}).get("token")
        return base64.b64decode(raw).decode() if raw else None
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "config_manager_controller.nautobot_token_lookup_failed",
            namespace=namespace,
            error=str(e),
        )
        return None


async def namespace_progress(
    namespace: str, kubeconfig: dict[str, Any] | None
) -> dict[str, Any]:
    """Summarise pods + notable events in ``namespace``.

    Never raises: an unreachable cluster is reported as a message, because
    this feeds a status panel that must not 500 mid-install.
    """
    try:
        if kubeconfig is None:
            await load_local_config()
        else:
            await load_kubeconfig_dict(kubeconfig)

        async with k8s_client.ApiClient() as api:
            core = k8s_client.CoreV1Api(api)
            pod_list = await core.list_namespaced_pod(namespace, timeout_seconds=10)
            pods: list[dict[str, Any]] = []
            for p in pod_list.items:
                ready, total, reason = _container_summary(p)
                pods.append(
                    {
                        "name": p.metadata.name,
                        "phase": p.status.phase or "Unknown",
                        "ready": ready,
                        "total": total,
                        "reason": reason,
                    }
                )
            pods.sort(key=lambda d: d["name"])

            events: list[dict[str, str]] = []
            try:
                ev_list = await core.list_namespaced_event(
                    namespace, timeout_seconds=10
                )
                seen: set[str] = set()
                for e in sorted(
                    ev_list.items,
                    key=lambda e: e.last_timestamp or e.event_time or "",
                    reverse=True,
                ):
                    if e.reason not in _INTERESTING_EVENT_REASONS:
                        continue
                    # One line per distinct message — a hundred identical
                    # BackOffs tell you nothing extra.
                    if e.message in seen:
                        continue
                    seen.add(e.message)
                    events.append(
                        {
                            "reason": e.reason or "",
                            "message": (e.message or "")[:300],
                            "object": (e.involved_object.name or "")
                            if e.involved_object
                            else "",
                        }
                    )
                    if len(events) >= 8:
                        break
            except Exception:  # noqa: BLE001
                # Events are best-effort; pods are the important half.
                logger.warning(
                    "config_manager_controller.progress_events_failed",
                    namespace=namespace,
                )

        summary = {
            "total": len(pods),
            "running": sum(1 for p in pods if p["phase"] == "Running"),
            "pending": sum(1 for p in pods if p["phase"] == "Pending"),
            "succeeded": sum(1 for p in pods if p["phase"] == "Succeeded"),
            "failed": sum(1 for p in pods if p["phase"] == "Failed"),
            "ready": sum(1 for p in pods if p["total"] and p["ready"] == p["total"]),
        }
        return {"pods": pods, "events": events, "summary": summary, "message": None}
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "config_manager_controller.progress_failed",
            namespace=namespace,
            error=str(e),
        )
        return {
            "pods": [],
            "events": [],
            "summary": {},
            # Before the install creates the namespace this is expected, so
            # phrase it as status rather than failure.
            "message": f"namespace not readable yet ({str(e)[:160]})",
        }
