"""Drive the gpu-controller to provision a tenant's vLLM GPU stack.

Mirrors ``core/configmgr/provision.py``: mints a ``gpu-provision``
service token, POSTs the desired spec to the controller, polls until
``active``, and returns the controller's view. The onboarding route
then writes the tenant's SOVEREIGN routing config so the LLM router
starts using the GPU.

vLLM serving is the existing ``deploy/k8s/gpu`` stack — this only drives
its deployment + tracks state. See docs/plans/2026-06-02-gpu-onboarding.md.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from daalu_automation.config import get_settings


class GpuProvisioningError(RuntimeError):
    pass


@dataclass
class ProvisionedGpu:
    state: str
    namespace: str
    model_classifier: str
    model_quality: str | None
    controller_row_id: str


def is_gpu_controller_enabled() -> bool:
    return bool(get_settings().gpu_controller_url)


async def list_gpu_nodes_via_controller(
    *,
    tenant_id: uuid.UUID,
    target_cluster_tunnel_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Ask the gpu-controller for schedulable GPU nodes on a cluster.

    Read-only; used by the onboarding UI to pre-fill the node/class.
    Returns [] (never raises) when the controller is unconfigured or the
    cluster is briefly unreachable — node hints are best-effort.
    """
    s = get_settings()
    if not s.gpu_controller_url:
        return []
    from daalu_automation.core.service_tokens import mint_service_token

    token = mint_service_token(
        tenant_id=str(tenant_id),
        user_id=str(tenant_id),
        purpose="gpu-provision",
        ttl_seconds=120,
    )
    base = s.gpu_controller_url.rstrip("/")
    params = {}
    if target_cluster_tunnel_id is not None:
        params["target_cluster_tunnel_id"] = str(target_cluster_tunnel_id)
    try:
        async with httpx.AsyncClient(
            timeout=15.0, headers={"Authorization": f"Bearer {token}"}
        ) as client:
            r = await client.get(f"{base}/tenants/{tenant_id}/gpu-nodes", params=params)
            if r.status_code != 200:
                return []
            return r.json().get("nodes", [])
    except Exception:  # noqa: BLE001 — best-effort hint
        return []


def _controller_headers(tenant_id: uuid.UUID, *, ttl_seconds: int = 900) -> dict[str, str]:
    from daalu_automation.core.service_tokens import mint_service_token

    token = mint_service_token(
        tenant_id=str(tenant_id),
        user_id=str(tenant_id),
        purpose="gpu-provision",
        ttl_seconds=ttl_seconds,
    )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _spec_body(
    *,
    gpu_node: str | None,
    gpu_class: str,
    hf_token: str | None,
    model_classifier: str,
    model_quality: str | None,
    target_cluster_tunnel_id: uuid.UUID | None,
    shared: bool,
) -> dict[str, Any]:
    return {
        "target_cluster_tunnel_id": (
            str(target_cluster_tunnel_id) if target_cluster_tunnel_id else None
        ),
        "gpu_node": gpu_node,
        "gpu_class": gpu_class,
        "hf_token": hf_token,
        "model_classifier": model_classifier,
        "model_quality": model_quality,
        "shared": shared,
    }


async def gpu_operator_status_via_controller(
    *, tenant_id: uuid.UUID, target_cluster_tunnel_id: uuid.UUID
) -> dict[str, Any]:
    """Is the NVIDIA GPU Operator on this cluster, and how far along?

    Raises :class:`GpuProvisioningError` with the controller's reason, so the
    form can say why (cluster not connected, controller not configured).
    """
    s = get_settings()
    if not s.gpu_controller_url:
        raise GpuProvisioningError("gpu_controller_url is not configured on this deploy")
    base = s.gpu_controller_url.rstrip("/")
    async with httpx.AsyncClient(
        timeout=30.0, headers=_controller_headers(tenant_id, ttl_seconds=120)
    ) as client:
        r = await client.get(
            f"{base}/tenants/{tenant_id}/gpu-operator",
            params={"target_cluster_tunnel_id": str(target_cluster_tunnel_id)},
        )
    if r.status_code != 200:
        raise GpuProvisioningError(_controller_detail(r))
    return r.json()


async def install_gpu_operator_via_controller(
    *, tenant_id: uuid.UUID, target_cluster_tunnel_id: uuid.UUID
) -> dict[str, Any]:
    """Start the operator install on the cluster (returns before it's done)."""
    s = get_settings()
    if not s.gpu_controller_url:
        raise GpuProvisioningError("gpu_controller_url is not configured on this deploy")
    base = s.gpu_controller_url.rstrip("/")
    async with httpx.AsyncClient(
        timeout=60.0, headers=_controller_headers(tenant_id, ttl_seconds=120)
    ) as client:
        r = await client.post(
            f"{base}/tenants/{tenant_id}/gpu-operator",
            json={"target_cluster_tunnel_id": str(target_cluster_tunnel_id)},
        )
    if r.status_code not in (200, 202):
        raise GpuProvisioningError(_controller_detail(r))
    return r.json()


async def pods_via_controller(
    *,
    tenant_id: uuid.UUID,
    target: str,
    target_cluster_tunnel_id: uuid.UUID | None,
    pod: str | None = None,
    container: str | None = None,
) -> dict[str, Any]:
    """Pods (``pod`` unset) or one pod's logs for the GPU Operator / model."""
    s = get_settings()
    if not s.gpu_controller_url:
        raise GpuProvisioningError("gpu_controller_url is not configured on this deploy")
    base = s.gpu_controller_url.rstrip("/")
    params: dict[str, str] = {"target": target}
    if target_cluster_tunnel_id is not None:
        params["target_cluster_tunnel_id"] = str(target_cluster_tunnel_id)
    path = "pods"
    if pod is not None:
        path = "pod-logs"
        params["pod"] = pod
        if container:
            params["container"] = container
    async with httpx.AsyncClient(
        timeout=20.0, headers=_controller_headers(tenant_id, ttl_seconds=120)
    ) as client:
        r = await client.get(f"{base}/tenants/{tenant_id}/{path}", params=params)
    if r.status_code != 200:
        raise GpuProvisioningError(_controller_detail(r))
    return r.json()


def _controller_detail(r: httpx.Response) -> str:
    try:
        detail = r.json().get("detail")
    except Exception:  # noqa: BLE001
        detail = None
    return str(detail or f"gpu-controller returned HTTP {r.status_code}")


async def gpu_progress_via_controller(
    *, tenant_id: uuid.UUID
) -> dict[str, Any]:
    """Pod phase + events + vLLM logs for the tenant's in-flight deploy.

    Best-effort like :func:`list_gpu_nodes_via_controller` — this backs a
    progress panel, and an empty panel is a better failure than a red error
    on a page whose actual job (showing the GPU) is working fine.
    """
    s = get_settings()
    if not s.gpu_controller_url:
        return {"pods": [], "events": [], "phase": None}
    base = s.gpu_controller_url.rstrip("/")
    try:
        async with httpx.AsyncClient(
            timeout=20.0, headers=_controller_headers(tenant_id, ttl_seconds=120)
        ) as client:
            r = await client.get(f"{base}/tenants/{tenant_id}/progress")
            if r.status_code != 200:
                return {"pods": [], "events": [], "phase": None}
            return r.json()
    except Exception:  # noqa: BLE001 — best-effort panel
        return {"pods": [], "events": [], "phase": None}


async def upsert_gpu_spec(
    *,
    tenant_id: uuid.UUID,
    gpu_node: str | None = None,
    gpu_class: str = "ada-16",
    hf_token: str | None = None,
    model_classifier: str = "meta/llama-3.1-8b-instruct",
    model_quality: str | None = None,
    target_cluster_tunnel_id: uuid.UUID | None = None,
    shared: bool = False,
) -> dict[str, Any]:
    """POST the desired spec to the controller and return its view. No polling.

    This is the half of provisioning that is actually fast and that can fail
    in ways the caller must see (bad spec, unreachable cluster, missing
    kubeconfig). The slow half — waiting for vLLM to pull weights and compile
    CUDA graphs — is the controller's reconcile loop's job, and the caller
    observes it via ``GET /onboarding/gpu`` rather than by holding an HTTP
    request open for it.

    Split out because the HTTP request the browser makes cannot survive that
    wait: it is minutes long, and the edge in front of the API times out at
    100s, so a *successful* deploy surfaced in the UI as ``Failed to fetch``.
    """
    s = get_settings()
    if not s.gpu_controller_url:
        raise GpuProvisioningError(
            "gpu_controller_url is not configured on this deploy"
        )
    base = s.gpu_controller_url.rstrip("/")
    body = _spec_body(
        gpu_node=gpu_node,
        gpu_class=gpu_class,
        hf_token=hf_token,
        model_classifier=model_classifier,
        model_quality=model_quality,
        target_cluster_tunnel_id=target_cluster_tunnel_id,
        shared=shared,
    )
    async with httpx.AsyncClient(
        timeout=30.0, headers=_controller_headers(tenant_id)
    ) as client:
        resp = await client.post(f"{base}/tenants/{tenant_id}", json=body)
        if resp.status_code not in (200, 201):
            raise GpuProvisioningError(
                f"gpu-controller rejected upsert: HTTP {resp.status_code} "
                f"— {resp.text[:300]}"
            )
        return resp.json()


async def provision_via_gpu_controller(
    *,
    tenant_id: uuid.UUID,
    gpu_node: str | None = None,
    gpu_class: str = "ada-16",
    hf_token: str | None = None,
    model_classifier: str = "meta/llama-3.1-8b-instruct",
    model_quality: str | None = None,
    target_cluster_tunnel_id: uuid.UUID | None = None,
    shared: bool = False,
    poll_deadline_s: int = 900,
) -> ProvisionedGpu:
    """Upsert + poll the gpu-controller until vLLM is ``active``.

    ``shared=True`` offers the stack to other tenants via the gateway; the
    controller rejects it (403) unless the tenant holds ``is_gpu_provider``.

    First boot pulls ~5 GB of weights + compiles CUDA graphs, so the
    default poll ceiling is generous (15 min).

    Blocking. Callers serving an HTTP request should use :func:`upsert_gpu_spec`
    instead and let the client poll — no browser-facing request survives a
    15-minute wait.
    """
    s = get_settings()
    if not s.gpu_controller_url:
        raise GpuProvisioningError(
            "gpu_controller_url is not configured on this deploy"
        )
    headers = _controller_headers(tenant_id)
    base = s.gpu_controller_url.rstrip("/")

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        view = await upsert_gpu_spec(
            tenant_id=tenant_id,
            gpu_node=gpu_node,
            gpu_class=gpu_class,
            hf_token=hf_token,
            model_classifier=model_classifier,
            model_quality=model_quality,
            target_cluster_tunnel_id=target_cluster_tunnel_id,
            shared=shared,
        )
        waited = 0
        while view.get("state") != "active":
            if view.get("state") == "error":
                raise GpuProvisioningError(
                    f"controller marked tenant errored: "
                    f"{view.get('last_error') or '(no detail)'}"
                )
            if waited >= poll_deadline_s:
                raise GpuProvisioningError(
                    f"vLLM stack did not reach 'active' in {poll_deadline_s}s "
                    f"(currently '{view.get('state')}'); still booting — retry soon"
                )
            await asyncio.sleep(5)
            waited += 5
            r = await client.get(f"{base}/tenants/{tenant_id}")
            if r.status_code != 200:
                raise GpuProvisioningError(
                    f"controller poll failed: HTTP {r.status_code}"
                )
            view = r.json()

    return ProvisionedGpu(
        state=view.get("state", "active"),
        namespace=view.get("namespace", "daalu"),
        model_classifier=view.get("model_classifier", model_classifier),
        model_quality=view.get("model_quality"),
        controller_row_id=view["id"],
    )
