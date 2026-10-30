"""FastAPI app + reconcile loop for the gpu-controller.

Shaped after ``nautobot_controller/app.py``: a small REST surface that
daalu-api drives, plus a background reconciler that brings K8s state in
line with the ``gpu_tenants`` DB table by applying the existing
``deploy/k8s/gpu`` vLLM stack onto the operator's or a customer's cluster.

Endpoints (all behind a service-token JWT signed with
``SERVICE_TOKEN_SECRET_KEY``, ``purpose='gpu-provision'``):

* ``POST   /tenants/{tenant_id}`` — get-or-create a GpuTenant row + kick off. Idempotent.
* ``GET    /tenants/{tenant_id}`` — read the current row.
* ``DELETE /tenants/{tenant_id}`` — schedule teardown.

The reconcile loop sweeps every 30 s; it is the only thing that talks to
the K8s API. Endpoints never block on it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from kubernetes_asyncio import client as k8s_client
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import get_settings
from daalu_automation.core import object_storage
from daalu_automation.core.crypto import decrypt_secret, encrypt_secret
from daalu_automation.core.service_tokens import (
    ServiceTokenClaims,
    ServiceTokenError,
    verify_service_token,
)
from daalu_automation.database import get_db
from daalu_automation.gpu_controller import aiperf as aiperf_build
from daalu_automation.gpu_controller import diagnostics as diag
from daalu_automation.gpu_controller import k8s as k8s_helpers
from daalu_automation.gpu_controller import manifests
from daalu_automation.models.aiperf_run import AiperfRun, AiperfRunState
from daalu_automation.models.cluster_tunnel import (
    ClusterTunnel,
    ClusterTunnelStatus,
)
from daalu_automation.models.gpu_diagnostic_run import (
    GpuDiagnosticKind,
    GpuDiagnosticRun,
    GpuDiagnosticState,
)
from daalu_automation.models.gpu_tenant import GpuTenant, GpuTenantState
from daalu_automation.models.integration import Integration
from daalu_automation.models.tenant import Tenant

logger = structlog.get_logger(__name__)

_RECONCILE_INTERVAL = 30.0
_SERVICE_TOKEN_PURPOSE = "gpu-provision"


class GpuSpec(BaseModel):
    """Caller-supplied desired state for a tenant's vLLM stack."""

    target_cluster_tunnel_id: uuid.UUID | None = None
    gpu_node: str | None = None
    gpu_class: str = "ada-16"
    hf_token: str | None = None
    model_classifier: str = "meta/llama-3.1-8b-instruct"
    model_quality: str | None = None
    # Offer this stack to OTHER tenants via the inference-gateway. Only
    # permitted when the owning tenant holds ``Tenant.is_gpu_provider``;
    # otherwise the stack is the owner's private SOVEREIGN endpoint.
    shared: bool = False


class GpuView(BaseModel):
    id: str
    tenant_id: str
    state: str
    target_cluster_tunnel_id: str | None
    namespace: str
    gpu_class: str
    gpu_node: str | None
    model_classifier: str
    model_quality: str | None
    shared: bool
    last_error: str | None
    last_ready_at: str | None = None


def _authenticate(authorization: str | None) -> ServiceTokenClaims:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = verify_service_token(authorization.split(None, 1)[1])
    except ServiceTokenError as e:
        logger.warning("gpu_controller.auth_failed", error=str(e))
        raise HTTPException(status_code=401, detail="invalid token") from e
    if claims.purpose != _SERVICE_TOKEN_PURPOSE:
        raise HTTPException(status_code=403, detail="wrong token purpose")
    return claims


def create_app() -> FastAPI:
    app = FastAPI(
        title="Daalu GPU Controller",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.on_event("startup")
    async def _startup() -> None:
        app.state.reconcile_task = asyncio.create_task(_reconcile_loop())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        app.state.reconcile_task.cancel()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/tenants/{tenant_id}", response_model=GpuView)
    async def upsert_tenant(
        tenant_id: uuid.UUID,
        spec: GpuSpec,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> GpuView:
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="tenant not found")

        # Provider guard — only a granted GPU provider may share a card.
        # Defence in depth: the gpu_tenants DB trigger enforces the same
        # invariant, but refuse early with a clear 403 here.
        if spec.shared and not tenant.is_gpu_provider:
            raise HTTPException(
                status_code=403,
                detail="tenant is not a granted GPU provider — cannot share a card",
            )

        if spec.target_cluster_tunnel_id is not None:
            ct = await db.get(ClusterTunnel, spec.target_cluster_tunnel_id)
            if ct is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"cluster_tunnel {spec.target_cluster_tunnel_id} not found",
                )
            if ct.tenant_id != tenant_id:
                raise HTTPException(
                    status_code=403,
                    detail="cluster_tunnel belongs to a different tenant",
                )
            if ct.status != ClusterTunnelStatus.connected:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"cluster_tunnel is {ct.status.value} — must be "
                        "'connected' before deploying vLLM to the customer cluster"
                    ),
                )

        row = await _find_row(db, tenant_id)
        if row is None:
            row = GpuTenant(
                tenant_id=tenant_id,
                state=GpuTenantState.pending,
                target_cluster_tunnel_id=spec.target_cluster_tunnel_id,
                namespace="daalu",
                gpu_class=spec.gpu_class,
                gpu_node=spec.gpu_node,
                model_classifier=spec.model_classifier,
                model_quality=spec.model_quality,
                shared=spec.shared,
                hf_token_ciphertext=(
                    encrypt_secret(spec.hf_token) if spec.hf_token else None
                ),
            )
            db.add(row)
        else:
            _respec(row, spec)
        await db.commit()
        await db.refresh(row)
        return _to_view(row)

    @app.get("/tenants/{tenant_id}", response_model=GpuView)
    async def get_tenant(
        tenant_id: uuid.UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> GpuView:
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        row = await _find_row(db, tenant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no gpu tenant")
        return _to_view(row)

    @app.get("/tenants/{tenant_id}/gpu-nodes")
    async def gpu_nodes(
        tenant_id: uuid.UUID,
        target_cluster_tunnel_id: uuid.UUID | None = None,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, Any]:
        """List schedulable GPU nodes on the target cluster (for the UI)."""
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        async with _ApiClientForTunnel(db, target_cluster_tunnel_id) as api:
            nodes = await k8s_helpers.list_gpu_nodes(api)
        return {"nodes": nodes}

    @app.get("/tenants/{tenant_id}/progress")
    async def vllm_progress(
        tenant_id: uuid.UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, Any]:
        """Pod phase + kubelet events + vLLM logs for an in-flight deploy.

        Backs the "what is it actually doing?" panel. A first boot spends
        minutes each on image pull, weight download and CUDA-graph compile,
        which are indistinguishable from outside — and before the container
        starts there are no logs at all, only events.
        """
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        row = await _find_row(db, tenant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no gpu tenant")
        async with _api_client_for(db, row) as api:
            return await k8s_helpers.vllm_progress(api, row.namespace)

    @app.delete("/tenants/{tenant_id}")
    async def destroy_tenant(
        tenant_id: uuid.UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, str]:
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        row = await _find_row(db, tenant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no gpu tenant")
        row.state = GpuTenantState.deleting
        await db.commit()
        return {"status": "scheduled"}

    return app


# ── Helpers ────────────────────────────────────────────────────────────


# States a new spec may replace. Disconnect leaves the row behind as
# ``destroyed`` (one row per tenant), so a reconnect lands here too; without
# it the POST answered 202 and nothing was ever deployed. Live stacks
# (provisioning/active/deleting) keep their spec: disconnect first to move.
_RESPECABLE = (GpuTenantState.pending, GpuTenantState.error, GpuTenantState.destroyed)


def _respec(row: GpuTenant, spec: GpuSpec) -> None:
    """Apply ``spec`` to an existing row and queue it for the reconciler."""
    if row.state not in _RESPECABLE:
        return
    row.target_cluster_tunnel_id = spec.target_cluster_tunnel_id
    row.gpu_class = spec.gpu_class
    row.gpu_node = spec.gpu_node
    row.model_classifier = spec.model_classifier
    row.model_quality = spec.model_quality
    row.shared = spec.shared
    if spec.hf_token:
        row.hf_token_ciphertext = encrypt_secret(spec.hf_token)
    row.state = GpuTenantState.pending
    row.last_error = None


async def _find_row(db: AsyncSession, tenant_id: uuid.UUID) -> GpuTenant | None:
    return (
        await db.execute(select(GpuTenant).where(GpuTenant.tenant_id == tenant_id))
    ).scalar_one_or_none()


def _to_view(row: GpuTenant) -> GpuView:
    return GpuView(
        id=str(row.id),
        tenant_id=str(row.tenant_id),
        state=row.state.value,
        target_cluster_tunnel_id=(
            str(row.target_cluster_tunnel_id)
            if row.target_cluster_tunnel_id
            else None
        ),
        namespace=row.namespace,
        gpu_class=row.gpu_class,
        gpu_node=row.gpu_node,
        model_classifier=row.model_classifier,
        model_quality=row.model_quality,
        shared=row.shared,
        last_error=row.last_error,
        last_ready_at=(
            row.last_ready_at.isoformat() if row.last_ready_at else None
        ),
    )


# ── Reconcile loop ─────────────────────────────────────────────────────


async def _reconcile_loop() -> None:
    from daalu_automation.database import AsyncSessionLocal

    logger.info("gpu_controller.reconcile_loop_started")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                rows = (
                    (
                        await db.execute(
                            select(GpuTenant).where(
                                GpuTenant.state != GpuTenantState.destroyed
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    try:
                        await _reconcile_one(db, row)
                    except Exception as e:  # noqa: BLE001
                        logger.exception(
                            "gpu_controller.reconcile_row_failed",
                            tenant_id=str(row.tenant_id),
                        )
                        row.last_error = str(e)[:1000]
                        await db.commit()
                await _reconcile_diagnostics(db)
                await _reconcile_aiperf(db)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("gpu_controller.reconcile_iteration_failed")
        await asyncio.sleep(_RECONCILE_INTERVAL)


async def _reconcile_one(db: AsyncSession, row: GpuTenant) -> None:
    if row.state == GpuTenantState.deleting:
        await _do_destroy(db, row)
        return

    if row.state in (GpuTenantState.pending, GpuTenantState.error):
        try:
            await _do_apply(db, row)
            row.state = GpuTenantState.provisioning
            row.last_error = None
            await db.commit()
        except Exception as e:  # noqa: BLE001
            row.state = GpuTenantState.error
            row.last_error = str(e)[:1000]
            await db.commit()
            raise
        return

    if row.state == GpuTenantState.provisioning:
        async with _api_client_for(db, row) as api:
            ready = await k8s_helpers.vllm_deployment_ready(api, row.namespace)
        if ready:
            row.state = GpuTenantState.active
            row.last_ready_at = datetime.now(tz=timezone.utc)
            await db.commit()
            logger.info(
                "gpu_controller.tenant_active", tenant_id=str(row.tenant_id)
            )
        return

    if row.state == GpuTenantState.active:
        # Drift correction — re-apply each tick (idempotent).
        try:
            await _do_apply(db, row)
        except Exception as e:  # noqa: BLE001
            row.last_error = str(e)[:1000]
            await db.commit()
        else:
            # Successful re-apply → the tunnel/cluster is reachable again.
            # Clear any stale error and refresh the heartbeat so the hub UI
            # (billing/local-gpu, AI Factory) reflects a healthy GPU instead
            # of pinning an old "Cannot connect …" message forever.
            if row.last_error is not None:
                row.last_error = None
            row.last_ready_at = datetime.now(tz=timezone.utc)
            await db.commit()


async def _do_apply(db: AsyncSession, row: GpuTenant) -> None:
    params = manifests.classifier_params(
        row.model_classifier,
        gpu_memory_utilization=row.gpu_memory_utilization,
        namespace=row.namespace,
        gpu_class=row.gpu_class,
        hf_token=(
            decrypt_secret(row.hf_token_ciphertext)
            if row.hf_token_ciphertext
            else ""
        ),
        # Stamp the owning tenant id onto the pod's ``tenant`` label so
        # dcgm-exporter + the nvidia-dcgm ServiceMonitor relabeling carry it
        # onto the DCGM series — the per-tenant scope the AI-factory UI queries.
        tenant_label=str(row.tenant_id),
    )
    bundle = manifests.build_all(params)
    async with _api_client_for(db, row) as api:
        # The vLLM Deployment selects on ``gpu-class``, which no stock
        # GPU-operator install creates — so stamp it ourselves rather than
        # making the operator label the node by hand and, when they forget,
        # letting the pod sit Pending until the provision poll times out with
        # nothing in the UI to explain it. Best-effort: if the credential
        # can't patch nodes, fall through and let scheduling report the real
        # problem instead of masking it as a permissions error.
        await _ensure_node_labelled(api, row)
        for m in bundle:
            await k8s_helpers.apply_one(api, m)


async def _ensure_node_labelled(api: Any, row: GpuTenant) -> None:
    """Label the target node with the tenant's ``gpu_class`` before applying.

    Targets ``row.gpu_node`` when the operator pinned one; otherwise labels
    every GPU node whose derived class matches, so a single-GPU cluster (the
    common case) needs no node selection at all.
    """
    try:
        if row.gpu_node:
            await k8s_helpers.ensure_gpu_class_label(api, row.gpu_node, row.gpu_class)
            return
        for node in await k8s_helpers.list_gpu_nodes(api):
            if node.get("gpu_class") == row.gpu_class:
                await k8s_helpers.ensure_gpu_class_label(
                    api, node["name"], row.gpu_class
                )
    except Exception as e:  # noqa: BLE001 — never block the apply
        logger.warning(
            "gpu_controller.node_label_failed",
            tenant_id=str(row.tenant_id),
            error=str(e)[:300],
        )


async def _do_destroy(db: AsyncSession, row: GpuTenant) -> None:
    """Delete the vLLM stack. We delete the workloads, not the whole
    namespace, since ``daalu`` may host other tenant resources."""
    params = manifests.GpuParams(namespace=row.namespace)
    async with _api_client_for(db, row) as api:
        for m in reversed(manifests.build_all(params)):
            if m["kind"] == "Namespace":
                continue  # never delete the shared namespace
            try:
                await k8s_helpers.delete_one(api, m)
            except k8s_client.exceptions.ApiException as e:
                if e.status != 404:
                    raise
    row.state = GpuTenantState.destroyed
    await db.commit()
    logger.info("gpu_controller.tenant_destroyed", tenant_id=str(row.tenant_id))


# ── Diagnostics runner (dcgmi diag / nccl) ─────────────────────────────


async def _reconcile_diagnostics(db: AsyncSession) -> None:
    """Run pending on-GPU diagnostics. ``observability_validate`` is resolved
    by daalu-api (read-only), so the controller only handles the exec kinds.

    Gated by ``settings.gpu_diagnostics_exec_enabled`` — when off, pending exec
    runs are failed fast with a clear message instead of left hanging.
    """
    pending = (
        (
            await db.execute(
                select(GpuDiagnosticRun).where(
                    GpuDiagnosticRun.state == GpuDiagnosticState.pending,
                    GpuDiagnosticRun.kind
                    != GpuDiagnosticKind.observability_validate,
                )
            )
        )
        .scalars()
        .all()
    )
    if not pending:
        return
    exec_enabled = get_settings().gpu_diagnostics_exec_enabled
    for run in pending:
        try:
            if not exec_enabled:
                _finish(
                    run,
                    GpuDiagnosticState.error,
                    {
                        "error": "On-GPU diagnostic exec is disabled "
                        "(gpu_diagnostics_exec_enabled=false). It is gated off by "
                        "default because dcgmi diag exercises the GPU and this is a "
                        "single shared card serving live traffic. Enable the flag to "
                        "run it (levels 2-3 stress the card — prefer a maintenance "
                        "window).",
                        "code": "exec_disabled",
                    },
                )
                await db.commit()
                continue
            await _run_one_diagnostic(db, run)
        except Exception as e:  # noqa: BLE001
            _finish(run, GpuDiagnosticState.error, {"error": str(e)[:500]})
            await db.commit()


def _finish(run: GpuDiagnosticRun, state: GpuDiagnosticState, summary: dict) -> None:
    run.state = state
    run.summary = summary
    run.finished_at = datetime.now(tz=timezone.utc)


async def _run_one_diagnostic(db: AsyncSession, run: GpuDiagnosticRun) -> None:
    gpu_row = None
    if run.gpu_tenant_id is not None:
        gpu_row = await db.get(GpuTenant, run.gpu_tenant_id)
    if gpu_row is None:
        gpu_row = await _find_row(db, run.tenant_id)
    if gpu_row is None:
        _finish(run, GpuDiagnosticState.error, {"error": "no GPU stack for tenant"})
        await db.commit()
        return

    run.state = GpuDiagnosticState.running
    run.started_at = datetime.now(tz=timezone.utc)
    await db.commit()

    if run.kind == GpuDiagnosticKind.nccl_test:
        # nccl-tests measure a collective — only meaningful on a multi-GPU node.
        # The single-card Ada has none (doc 02 §4.2); the Job builder
        # (diag.nccl_all_reduce_job) is ready for a future multi-GPU tenant.
        _finish(
            run,
            GpuDiagnosticState.error,
            {"error": "nccl-tests require a multi-GPU node; N/A on the single-card Ada"},
        )
        await db.commit()
        return

    # dcgmi diag — the dcgm-exporter image ships no dcgmi CLI, so run a one-shot
    # DCGM Job on the GPU node, read its log, and clean it up.
    job = diag.dcgmi_diag_job(
        gpu_row.namespace,
        level=run.level,
        gpu_class=gpu_row.gpu_class,
        node=gpu_row.gpu_node or None,
        name=f"daalu-dcgmi-diag-{str(run.id)[:8]}",
        image=get_settings().gpu_dcgm_diag_image or None,
    )
    async with _api_client_for(db, gpu_row) as api:
        try:
            stdout = await _run_job_capture(
                api, gpu_row.namespace, job, timeout_s=1500
            )
        except Exception as e:  # noqa: BLE001
            # Surface the actual failure (image pull, no free GPU slice, RBAC,
            # timeout …) verbatim so the UI shows a real message + code.
            msg = str(e)
            run.output = msg[-8000:]
            _finish(
                run,
                GpuDiagnosticState.error,
                {
                    "error": f"dcgmi diag job failed: {msg[:500]}",
                    "command": " ".join(diag.dcgmi_diag_cmd(run.level)),
                },
            )
            await db.commit()
            return
    passed, summary = diag.parse_dcgmi_output(stdout)
    run.output = (stdout or "")[-8000:]
    _finish(
        run,
        GpuDiagnosticState.passed if passed else GpuDiagnosticState.failed,
        summary,
    )
    await db.commit()


# ── AIPerf runner (load-test / SLO benchmarking) ───────────────────────


async def _reconcile_aiperf(db: AsyncSession) -> None:
    """Run pending AIPerf sweeps as one-shot Jobs on the OPERATOR cluster.

    Gated by ``settings.gpu_aiperf_exec_enabled`` — off by default because a
    full-concurrency sweep IS load on the single shared prod card (it spikes
    live tenants). When off, pending runs are failed fast with a clear message.
    """
    pending = (
        (
            await db.execute(
                select(AiperfRun).where(AiperfRun.state == AiperfRunState.pending)
            )
        )
        .scalars()
        .all()
    )
    if not pending:
        return
    exec_enabled = get_settings().gpu_aiperf_exec_enabled
    for run in pending:
        try:
            if not exec_enabled:
                _finish_aiperf(
                    run,
                    AiperfRunState.error,
                    {
                        "error": "AIPerf exec is disabled "
                        "(gpu_aiperf_exec_enabled=false). It is gated off by "
                        "default because a full concurrency sweep loads the "
                        "endpoint under test — against the single shared prod "
                        "card it spikes live tenants. Enable the flag and run "
                        "off-peak (or against a candidate node).",
                        "code": "exec_disabled",
                    },
                )
                await db.commit()
                continue
            await _run_one_aiperf(db, run)
        except Exception as e:  # noqa: BLE001
            _finish_aiperf(run, AiperfRunState.error, {"error": str(e)[:500]})
            await db.commit()


def _finish_aiperf(run: AiperfRun, state: AiperfRunState, summary: dict) -> None:
    run.state = state
    run.summary = summary
    run.finished_at = datetime.now(tz=timezone.utc)


async def _run_one_aiperf(db: AsyncSession, run: AiperfRun) -> None:
    run.state = AiperfRunState.running
    run.started_at = datetime.now(tz=timezone.utc)
    await db.commit()

    settings = get_settings()
    # Object-storage destination for the artifact tree. The uploader sidecar
    # mirrors /artifacts here under aiperf/{run_id}/ so the structured outputs
    # (profile_export_aiperf.csv/json + logs) survive the pod. The sidecar runs
    # ON the cluster under test, so it writes to the IN-CLUSTER endpoint
    # (gpu_aiperf_s3_endpoint); creds fall back to the platform s3_* for the
    # single-cluster/dev case.
    prefix = f"aiperf/{run.id}"
    artifacts_s3 = {
        "endpoint": settings.gpu_aiperf_s3_endpoint or settings.s3_endpoint_url,
        "access_key": settings.gpu_aiperf_s3_access_key or settings.s3_access_key,
        "secret_key": settings.gpu_aiperf_s3_secret_key or settings.s3_secret_key,
        "bucket": settings.s3_bucket_aiperf,
        "prefix": prefix,
    }
    job = aiperf_build.aiperf_bench_job(
        "daalu",
        model=run.model,
        url=run.target_url,
        concurrency=run.concurrency,
        request_count=run.request_count,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        endpoint_type=run.endpoint_type,
        streaming=run.streaming,
        name=f"daalu-aiperf-{str(run.id)[:8]}",
        image=settings.gpu_aiperf_image or None,
        artifacts_s3=artifacts_s3,
        uploader_image=settings.gpu_aiperf_uploader_image or None,
    )
    # AIPerf is a client that opens connections to an in-cluster Service on the
    # cluster that HOSTS the endpoint under test — the same cluster the run's
    # GpuTenant stack lives on (its llm-classifier / gateway). Resolve that
    # cluster's tunnel (None → the operator/hub cluster) so the Job runs where
    # the GPU + Service + object store actually are, not on the hub.
    tunnel_id = await _aiperf_cluster_tunnel(db, run)
    async with _ApiClientForTunnel(db, tunnel_id) as api:
        try:
            # Multi-container pod (aiperf + uploader) — read the aiperf log.
            stdout = await _run_job_capture(
                api, "daalu", job, timeout_s=3300, container="aiperf"
            )
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            run.output = msg[-8000:]
            _finish_aiperf(
                run,
                AiperfRunState.error,
                {"error": f"aiperf job failed: {msg[:500]}", "code": "job_failed"},
            )
            await db.commit()
            return
    passed, summary = aiperf_build.parse_aiperf_output(stdout)
    run.output = (stdout or "")[-8000:]
    # Pull the uploaded artifact tree back: list + parse the per-concurrency
    # JSONs into the SLO curve, and record the downloadable file manifest. A run
    # that produced a usable curve counts as passed even if stdout didn't parse.
    extra = await _collect_aiperf_artifacts(run.id, settings.s3_bucket_aiperf, prefix)
    summary.update(extra)
    if not passed and extra.get("concurrency_levels"):
        passed = True
        summary.pop("error", None)
    _finish_aiperf(
        run,
        AiperfRunState.passed if passed else AiperfRunState.failed,
        summary,
    )
    await db.commit()


async def _aiperf_cluster_tunnel(
    db: AsyncSession, run: AiperfRun
) -> uuid.UUID | None:
    """The cluster the AIPerf Job runs on = where the endpoint under test lives.

    Resolve the run tenant's GpuTenant stack; its ``target_cluster_tunnel_id``
    names the workload/operator cluster (None → the hub). For a site superuser
    the run tenant is the operator tenant, which owns the shared stack; for an
    owner/provider it is their own card's cluster. No GpuTenant → hub.
    """
    gt = (
        await db.execute(
            select(GpuTenant).where(GpuTenant.tenant_id == run.tenant_id)
        )
    ).scalar_one_or_none()
    return gt.target_cluster_tunnel_id if gt else None


async def _collect_aiperf_artifacts(
    run_id: uuid.UUID, bucket: str, prefix: str
) -> dict:
    """List the uploaded artifact tree from object storage, parse the
    per-concurrency JSONs into a curve, and return the summary fragment
    (``artifacts`` manifest + ``concurrency_levels``). Best-effort — a storage
    error degrades to ``artifacts_error`` rather than failing the run.

    Reads via the AIPerf client (the workload store reached over the tunnel),
    not the platform s3_*."""
    base = f"{prefix}/"
    client = object_storage.aiperf_client()
    try:
        objs = await asyncio.to_thread(
            object_storage.list_objects, bucket, base, client
        )
    except Exception as e:  # noqa: BLE001 — storage is optional, never fatal
        logger.warning(
            "aiperf_artifacts_list_failed", run_id=str(run_id), error=str(e)
        )
        return {"artifacts_error": str(e)[:300]}
    if not objs:
        return {}
    artifacts = [
        {"path": o["key"][len(base):], "size": o["size"]}
        for o in objs
        if o["key"] != base
    ]
    # Download only the per-concurrency report JSONs (bounded) for the curve.
    files: dict[str, bytes] = {}
    for o in objs:
        rel = o["key"][len(base):]
        if rel.endswith("profile_export_aiperf.json") and o["size"] <= 2_000_000:
            try:
                files[rel] = await asyncio.to_thread(
                    object_storage.get_object_bytes, bucket, o["key"], client
                )
            except Exception:  # noqa: BLE001 — skip a file we can't fetch
                continue
    out: dict[str, Any] = {"artifacts": artifacts, "artifacts_bucket": bucket}
    levels = aiperf_build.parse_aiperf_artifacts(files)
    if levels:
        out["concurrency_levels"] = levels
    return out


async def _run_job_capture(
    api: k8s_client.ApiClient,
    namespace: str,
    job: dict,
    *,
    timeout_s: float,
    container: str | None = None,
) -> str:
    """Create a one-shot Job, wait for it to finish (or time out), return its
    pod log, and delete it. Raises on create failure.

    ``container`` names which container's log to read — required when the pod
    has more than one (e.g. the AIPerf pod's aiperf + uploader sidecar)."""
    batch = k8s_client.BatchV1Api(api)
    core = k8s_client.CoreV1Api(api)
    name = job["metadata"]["name"]

    # Clear any stale Job of the same name first (idempotent re-runs).
    try:
        await batch.delete_namespaced_job(
            name, namespace, propagation_policy="Background"
        )
        await asyncio.sleep(2)
    except k8s_client.exceptions.ApiException:
        pass

    await batch.create_namespaced_job(namespace, job)
    try:
        pod_name: str | None = None
        for _ in range(max(1, int(timeout_s // 5))):
            await asyncio.sleep(5)
            if pod_name is None:
                pods = await core.list_namespaced_pod(
                    namespace, label_selector=f"job-name={name}"
                )
                if pods.items:
                    pod_name = pods.items[0].metadata.name
            status = (await batch.read_namespaced_job_status(name, namespace)).status
            if status and (status.succeeded or status.failed):
                break
        if pod_name is None:
            return (
                "(diagnostic pod never scheduled within the timeout — no free GPU "
                "slice on the node, or the image could not be pulled)"
            )
        try:
            kw: dict[str, Any] = {"_preload_content": True}
            if container is not None:
                kw["container"] = container
            return await core.read_namespaced_pod_log(pod_name, namespace, **kw)
        except k8s_client.exceptions.ApiException as e:
            return f"(could not read diagnostic pod log: {e})"
    finally:
        try:
            await batch.delete_namespaced_job(
                name, namespace, propagation_policy="Background"
            )
        except k8s_client.exceptions.ApiException:
            pass


# ── K8s client factory (operator vs customer cluster) ──────────────────


class _ApiClientContext:
    def __init__(self, db: AsyncSession, row: GpuTenant) -> None:
        self.db = db
        self.row = row
        self.api: k8s_client.ApiClient | None = None

    async def __aenter__(self) -> k8s_client.ApiClient:
        if self.row.target_cluster_tunnel_id is None:
            await k8s_helpers.load_local_config()
        else:
            kubeconfig = await _load_customer_kubeconfig(
                self.db, self.row.target_cluster_tunnel_id
            )
            await k8s_helpers.load_kubeconfig_dict(kubeconfig)
        self.api = k8s_client.ApiClient()
        return self.api

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.api is not None:
            await self.api.close()


def _api_client_for(db: AsyncSession, row: GpuTenant) -> _ApiClientContext:
    return _ApiClientContext(db, row)


class _ApiClientForTunnel:
    """Build a K8s client for an arbitrary cluster (by tunnel id, or the
    operator cluster when None) — used for read-only node discovery before
    a GpuTenant row exists."""

    def __init__(self, db: AsyncSession, tunnel_id: uuid.UUID | None) -> None:
        self.db = db
        self.tunnel_id = tunnel_id
        self.api: k8s_client.ApiClient | None = None

    async def __aenter__(self) -> k8s_client.ApiClient:
        if self.tunnel_id is None:
            await k8s_helpers.load_local_config()
        else:
            kubeconfig = await _load_customer_kubeconfig(self.db, self.tunnel_id)
            await k8s_helpers.load_kubeconfig_dict(kubeconfig)
        self.api = k8s_client.ApiClient()
        return self.api

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.api is not None:
            await self.api.close()


async def _load_customer_kubeconfig(
    db: AsyncSession, tunnel_id: uuid.UUID
) -> dict[str, Any]:
    """Fetch the kubeconfig from the joined cluster's sibling
    ``Integration(provider="kubernetes")`` row (same source the
    nautobot-controller uses)."""
    tunnel = await db.get(ClusterTunnel, tunnel_id)
    if tunnel is None:
        raise RuntimeError(
            f"cluster_tunnel {tunnel_id} missing — cannot reach customer cluster"
        )
    # Prefer the kubeconfig explicitly linked to *this* cluster — the edge
    # self-registers one per tunnel at bootstrap, so a tenant with several
    # joined clusters has several rows and the unscoped lookup below would
    # pick an arbitrary one (and drive the GPU stack into the wrong cluster).
    # Fall back to an unlinked row for tenants onboarded before
    # self-registration, whose single kubeconfig was pasted by hand.
    integ = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tunnel.tenant_id,
                Integration.provider == "kubernetes",
                Integration.cluster_tunnel_id == tunnel.id,
            )
        )
    ).scalar_one_or_none()
    if integ is None:
        integ = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == tunnel.tenant_id,
                    Integration.provider == "kubernetes",
                    Integration.cluster_tunnel_id.is_(None),
                )
            )
        ).scalars().first()
    if integ is None or not (integ.config or {}).get("kubeconfig"):
        raise RuntimeError(
            f"no kubeconfig on cluster_tunnel {tunnel_id}'s sibling "
            "Integration(provider='kubernetes') — finish cluster onboarding "
            "before deploying the GPU stack there"
        )
    parsed = yaml.safe_load((integ.config or {}).get("kubeconfig") or "")
    if not isinstance(parsed, dict):
        raise RuntimeError("kubeconfig YAML did not parse to a dict")
    return parsed


app = create_app()
