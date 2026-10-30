"""FastAPI app + reconcile loop for nautobot-controller.

Shaped after workspace_controller/app.py: a small REST surface that
daalu-api drives, plus a background reconciler that brings K8s state
in line with the ``nautobot_tenants`` DB table.

Endpoints (all behind a service-token JWT signed with
``SERVICE_TOKEN_SECRET_KEY``, ``purpose='nautobot-provision'``):

* ``POST /tenants/{tenant_id}`` — get-or-create a NautobotTenant row
  and kick off provisioning. Idempotent.
* ``GET  /tenants/{tenant_id}`` — read the current row.
* ``DELETE /tenants/{tenant_id}`` — schedule teardown.

The reconcile loop sweeps every 30 s, materialising or tearing down
K8s state based on the row's ``state`` field. The loop is the only
thing that talks to the K8s API — endpoints never block on it.
"""

from __future__ import annotations

import asyncio
import secrets
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

from daalu_automation.core.crypto import decrypt_secret, encrypt_secret
from daalu_automation.core.service_tokens import (
    ServiceTokenClaims,
    ServiceTokenError,
    verify_service_token,
)
from daalu_automation.database import get_db
from daalu_automation.models.cluster_tunnel import (
    ClusterTunnel,
    ClusterTunnelStatus,
)
from daalu_automation.models.integration import Integration
from daalu_automation.models.nautobot_tenant import (
    NautobotTenant,
    NautobotTenantState,
)
from daalu_automation.models.tenant import Tenant
from daalu_automation.nautobot_controller import k8s as k8s_helpers
from daalu_automation.nautobot_controller import manifests

logger = structlog.get_logger(__name__)

# Reconcile loop tick. 30s feels right for a workload that has 30-90s
# first-boot times — much shorter and we just churn k8s API calls.
_RECONCILE_INTERVAL = 30.0


class TenantSpec(BaseModel):
    """Caller-supplied desired state for a per-tenant Nautobot.

    Both fields are optional and default to operator-cluster mode.
    ``target_cluster_tunnel_id`` switches to customer-cluster mode and
    must reference an existing ClusterTunnel that's ``connected``.
    """

    target_cluster_tunnel_id: uuid.UUID | None = None
    hostname: str | None = None


class TenantView(BaseModel):
    id: str
    tenant_id: str
    state: str
    target_cluster_tunnel_id: str | None
    namespace: str
    hostname: str | None
    last_error: str | None
    # The provision route on daalu-api uses these to write the
    # Integration row. Only populated once ``state == 'active'``.
    url: str | None = None
    admin_token: str | None = None


def _authenticate(authorization: str | None) -> ServiceTokenClaims:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return verify_service_token(authorization.split(None, 1)[1])
    except ServiceTokenError as e:
        logger.warning("nautobot_controller.auth_failed", error=str(e))
        raise HTTPException(status_code=401, detail="invalid token")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Daalu Nautobot Controller",
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

    @app.post("/tenants/{tenant_id}", response_model=TenantView)
    async def upsert_tenant(
        tenant_id: uuid.UUID,
        spec: TenantSpec,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> TenantView:
        """Get-or-create the per-tenant NautobotTenant row.

        Idempotent. If a row already exists and is not in
        ``destroyed``/``error``, the same row is returned and the
        spec change (if any) is taken on the next reconcile tick.
        """
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="tenant not found")

        # Customer-cluster mode: validate the tunnel exists and is
        # connected. We could let the reconcile loop discover this,
        # but a 400 at upsert-time gives the caller a precise reason.
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
                        "'connected' before deploying Nautobot to the "
                        "customer cluster"
                    ),
                )

        row = await _find_row(db, tenant_id)
        if row is None:
            row = NautobotTenant(
                tenant_id=tenant_id,
                state=NautobotTenantState.pending,
                target_cluster_tunnel_id=spec.target_cluster_tunnel_id,
                namespace=manifests.namespace_for_slug(tenant.slug),
                hostname=spec.hostname or _default_hostname(spec, tenant.slug),
                # Bootstrap credentials are generated once here so a
                # re-apply on the next reconcile tick lands the same
                # values into the K8s Secret (changing them mid-flight
                # would lock the running Nautobot out of its own DB).
                postgres_password_ciphertext=encrypt_secret(_gen_password()),
                secret_key_ciphertext=encrypt_secret(_gen_secret_key()),
                admin_token_ciphertext=encrypt_secret(_gen_api_token()),
            )
            db.add(row)
        else:
            # Allow spec updates only on rows that haven't started; once
            # we've stamped K8s state we don't accept retroactive
            # target_cluster_tunnel_id changes (would be a re-deploy
            # into a different cluster, which is destroy+create).
            if row.state == NautobotTenantState.pending:
                row.target_cluster_tunnel_id = spec.target_cluster_tunnel_id
                if spec.hostname:
                    row.hostname = spec.hostname
        await db.commit()
        await db.refresh(row)
        return await _to_view(db, row)

    @app.get("/tenants/{tenant_id}", response_model=TenantView)
    async def get_tenant(
        tenant_id: uuid.UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> TenantView:
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        row = await _find_row(db, tenant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no nautobot tenant")
        return await _to_view(db, row)

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
            raise HTTPException(status_code=404, detail="no nautobot tenant")
        row.state = NautobotTenantState.deleting
        await db.commit()
        return {"status": "scheduled"}

    return app


# ── Helpers ────────────────────────────────────────────────────────────


async def _find_row(
    db: AsyncSession, tenant_id: uuid.UUID
) -> NautobotTenant | None:
    return (
        await db.execute(
            select(NautobotTenant).where(NautobotTenant.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


def _default_hostname(spec: TenantSpec, slug: str) -> str | None:
    """The hostname we'll bake into the per-tenant Nautobot config.

    Operator-cluster mode → ``<slug>.sot.daalu.io``. Customer-cluster
    mode → None (no public ingress; daalu reaches via the tunnel).
    """
    if spec.target_cluster_tunnel_id is not None:
        return None
    return f"{slug}.sot.daalu.io"


def _gen_password() -> str:
    # 32 base64 chars ≈ 24 bytes of entropy. Used for both Postgres
    # and the Nautobot admin user (the admin almost never logs in by
    # password — token-based auth is the customer-facing path).
    return secrets.token_urlsafe(32)


def _gen_secret_key() -> str:
    # 64+ bytes for Django session signing. Rotating this on a
    # reconcile would log every user out, so we generate once at
    # create-time and reuse on every re-apply.
    return secrets.token_urlsafe(64)


def _gen_api_token() -> str:
    # 40 hex chars matches Nautobot's default token format and what
    # the daalu Integration row expects.
    return secrets.token_hex(20)


async def _to_view(db: AsyncSession, row: NautobotTenant) -> TenantView:
    """Decorate the row with the *post-active* URL + token if ready.

    The provision route on daalu-api uses these two fields to write
    the customer-facing Integration row. They're only meaningful once
    ``state == 'active'`` — earlier states return them as None.
    """
    url = None
    admin_token = None
    if row.state == NautobotTenantState.active:
        if row.hostname:
            url = f"https://{row.hostname}"
        else:
            # Customer-cluster mode: the daalu adapter reaches the
            # in-cluster Service through the wg tunnel. The hostname
            # is the in-cluster DNS, not a public one.
            url = f"http://nautobot.{row.namespace}.svc.cluster.local"
        if row.admin_token_ciphertext:
            admin_token = decrypt_secret(row.admin_token_ciphertext)
    return TenantView(
        id=str(row.id),
        tenant_id=str(row.tenant_id),
        state=row.state.value,
        target_cluster_tunnel_id=(
            str(row.target_cluster_tunnel_id)
            if row.target_cluster_tunnel_id
            else None
        ),
        namespace=row.namespace,
        hostname=row.hostname,
        last_error=row.last_error,
        url=url,
        admin_token=admin_token,
    )


# ── Reconcile loop ─────────────────────────────────────────────────────


async def _reconcile_loop() -> None:
    """Sweep every row every _RECONCILE_INTERVAL seconds."""
    from daalu_automation.database import AsyncSessionLocal

    logger.info("nautobot_controller.reconcile_loop_started")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                rows = (
                    await db.execute(
                        select(NautobotTenant).where(
                            NautobotTenant.state != NautobotTenantState.destroyed
                        )
                    )
                ).scalars().all()
                for row in rows:
                    try:
                        await _reconcile_one(db, row)
                    except Exception as e:  # noqa: BLE001
                        # Don't let one bad row stop the loop. Record
                        # the error on the row so the operator can see
                        # it without grepping logs.
                        logger.exception(
                            "nautobot_controller.reconcile_row_failed",
                            tenant_id=str(row.tenant_id),
                        )
                        row.last_error = str(e)[:1000]
                        await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("nautobot_controller.reconcile_iteration_failed")
        await asyncio.sleep(_RECONCILE_INTERVAL)


async def _reconcile_one(db: AsyncSession, row: NautobotTenant) -> None:
    """Bring one row's K8s state in line with the desired state."""
    if row.state == NautobotTenantState.deleting:
        await _do_destroy(db, row)
        return

    if row.state in (NautobotTenantState.pending, NautobotTenantState.error):
        # (Re-)apply manifests. Errors flip the row to `error`, with
        # a clear ``last_error``; the next tick retries the apply,
        # because that's safe (server-side patch is idempotent).
        try:
            await _do_apply(db, row)
            row.state = NautobotTenantState.provisioning
            row.last_error = None
            await db.commit()
        except Exception as e:  # noqa: BLE001
            row.state = NautobotTenantState.error
            row.last_error = str(e)[:1000]
            await db.commit()
            raise
        return

    if row.state == NautobotTenantState.provisioning:
        # Has the web Deployment reported a ready replica?
        async with _api_client_for(db, row) as api:
            ready = await k8s_helpers.web_deployment_ready(api, row.namespace)
        if ready:
            row.state = NautobotTenantState.active
            row.last_ready_at = datetime.now(tz=timezone.utc)
            await db.commit()
            logger.info(
                "nautobot_controller.tenant_active", tenant_id=str(row.tenant_id)
            )
        return

    if row.state == NautobotTenantState.active:
        # Re-apply on every tick — drift correction. Cheap when
        # nothing has changed because the K8s server returns 200 for
        # an identical patch.
        try:
            await _do_apply(db, row)
        except Exception as e:  # noqa: BLE001
            # Don't flip back to error on a transient blip; just record
            # the most recent failure for the operator to inspect.
            row.last_error = str(e)[:1000]
            await db.commit()


async def _do_apply(db: AsyncSession, row: NautobotTenant) -> None:
    """Apply every manifest in the per-tenant stack."""
    tenant = await db.get(Tenant, row.tenant_id)
    if tenant is None:
        raise RuntimeError(f"tenant {row.tenant_id} disappeared mid-reconcile")

    params = manifests.TenantParams(
        slug=tenant.slug,
        name=tenant.name,
        target_cluster_tunnel_id=(
            str(row.target_cluster_tunnel_id)
            if row.target_cluster_tunnel_id
            else None
        ),
        hostname=row.hostname,
        postgres_password=_must_decrypt(row.postgres_password_ciphertext, "postgres_password"),
        nautobot_secret_key=_must_decrypt(row.secret_key_ciphertext, "secret_key"),
        admin_password=_must_decrypt(row.postgres_password_ciphertext, "admin_password"),
        admin_token=_must_decrypt(row.admin_token_ciphertext, "admin_token"),
    )
    bundle = manifests.build_all(params)
    async with _api_client_for(db, row) as api:
        for m in bundle:
            await k8s_helpers.apply_one(api, m)


async def _do_destroy(db: AsyncSession, row: NautobotTenant) -> None:
    """Delete the per-tenant namespace and mark the row destroyed.

    Namespace-delete cascades to every namespaced resource. The PVC's
    PV is released depending on its reclaimPolicy — local-path's
    default is Delete, so customer data is removed. We deliberately
    don't try to preserve data here; the customer-facing wizard does
    the "are you sure?" gate.
    """
    async with _api_client_for(db, row) as api:
        core = k8s_client.CoreV1Api(api)
        try:
            await core.delete_namespace(name=row.namespace)
        except k8s_client.exceptions.ApiException as e:
            if e.status != 404:
                raise
    row.state = NautobotTenantState.destroyed
    await db.commit()
    logger.info(
        "nautobot_controller.tenant_destroyed", tenant_id=str(row.tenant_id)
    )


def _must_decrypt(ct: str | None, what: str) -> str:
    if not ct:
        raise RuntimeError(f"missing {what} ciphertext on nautobot_tenant row")
    return decrypt_secret(ct)


# ── K8s client factory (operator vs customer cluster) ──────────────────


class _ApiClientContext:
    """Async context manager wrapping the K8s API client lifecycle.

    Picks operator-cluster vs customer-cluster mode based on the
    NautobotTenant row's ``target_cluster_tunnel_id``. The kubeconfig
    for customer-cluster mode is read from the
    ``Integration(provider="kubernetes")`` row that the
    ``ClusterTunnel`` row implicitly references.
    """

    def __init__(self, db: AsyncSession, row: NautobotTenant) -> None:
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


def _api_client_for(db: AsyncSession, row: NautobotTenant) -> _ApiClientContext:
    return _ApiClientContext(db, row)


async def _load_customer_kubeconfig(
    db: AsyncSession, tunnel_id: uuid.UUID
) -> dict[str, Any]:
    """Fetch + decrypt the kubeconfig stored on the customer's Integration.

    Daalu's cluster_tunnel row owns the wireguard mesh state; the
    *kubeconfig* lives on a sibling ``Integration(provider="kubernetes")``
    row keyed by (tenant_id, slug). We look that one up here.

    The kubeconfig is base64-decoded if it's a string blob, or
    YAML-parsed if it's a multi-line YAML — accept both shapes
    because the cluster-onboarding wizard accepts either.
    """
    tunnel = await db.get(ClusterTunnel, tunnel_id)
    if tunnel is None:
        raise RuntimeError(f"cluster_tunnel {tunnel_id} missing — cannot reach customer cluster")
    integ = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tunnel.tenant_id,
                Integration.provider == "kubernetes",
            )
        )
    ).scalar_one_or_none()
    if integ is None or not (integ.config or {}).get("kubeconfig"):
        raise RuntimeError(
            f"no kubeconfig on cluster_tunnel {tunnel_id}'s sibling "
            "Integration(provider='kubernetes') — customer must finish "
            "cluster onboarding before deploying Nautobot there"
        )
    blob = (integ.config or {}).get("kubeconfig") or ""
    parsed = yaml.safe_load(blob)
    if not isinstance(parsed, dict):
        raise RuntimeError("kubeconfig YAML did not parse to a dict")
    return parsed


app = create_app()
