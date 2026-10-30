"""FastAPI app + reconcile loop for config-manager-controller.

Mirrors ``nautobot_controller/app.py``: a small REST surface daalu-api
drives (service-token JWT, ``purpose='config-manager-provision'``) plus a
background reconciler that converges ``config_manager_tenants`` rows to a
running NV-CM Helm release. Unlike the nautobot-controller, materialisation
is ``helm upgrade --install`` of the vendored pinned chart (via HelmRunner)
rather than raw-manifest apply.

Endpoints:
* ``POST /tenants/{tenant_id}`` — get-or-create the row + start provisioning.
* ``GET  /tenants/{tenant_id}`` — read state + (when active) the resolved URLs.
* ``DELETE /tenants/{tenant_id}`` — schedule teardown (helm uninstall).
* ``…/tenants/{tenant_id}/environments[/{env_id}]`` — the same lifecycle, but
  addressing one *named environment* out of the several a customer may run.

The singular ``/tenants/{tenant_id}…`` routes predate environments and are
kept working verbatim: they resolve the tenant's **primary** environment (see
``_find_row``), which is what every existing caller — the hub's onboarding
routes, the tool proxy, the SoT client — already means by "the tenant's config
manager".

See docs/design/nv-config-manager-integration.md §6.4.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.config import get_settings
from daalu_automation.config_manager_controller import (
    chart_versions,
    collectors,
    env_slug,
    local_admin,
    prechecks,
    progress,
)
from daalu_automation.config_manager_controller import values as values_mod
from daalu_automation.config_manager_controller.deployer_runner import DeployerRunner
from daalu_automation.config_manager_controller.helm_runner import HelmRunner
from daalu_automation.core.crypto import encrypt_secret
from daalu_automation.core.service_tokens import (
    ServiceTokenClaims,
    ServiceTokenError,
    verify_service_token,
)
from daalu_automation.database import get_db
from daalu_automation.models import (
    ClusterTunnel,
    ClusterTunnelStatus,
    ConfigManagerTenant,
    ConfigManagerTenantState,
    Tenant,
)

# Reuse the nautobot-controller's tunnel-kubeconfig loader — one place
# decides how to reach a customer cluster over the wg mesh.
from daalu_automation.nautobot_controller.app import _load_customer_kubeconfig

logger = structlog.get_logger(__name__)

_RECONCILE_INTERVAL = 30.0
_ACCEPTED_PURPOSE = "config-manager-provision"

# Consecutive-failure backoff for provisioning, keyed by environment id.
#
# An install that *cannot* succeed — a full node, a missing Tier-A operator, a
# host cluster that is down — used to be retried on every 30 s tick forever,
# and each attempt is a full multi-minute Deployer pass. Two such environments
# saturate this (sequential) loop end to end, so nothing else ever gets a turn:
# the tick that should have torn down a deleted environment is still grinding
# through a doomed provision. Backing off turns a permanent failure into a
# quiet one and hands the loop back.
#
# Held in memory rather than on the row: it describes this process's retry
# cadence, not the environment, and a controller restart is exactly when an
# immediate retry is wanted. Anything that moves a row back to `pending` (a
# create, an upgrade) clears it — that is a human asking for a retry now.
_MIN_BACKOFF = 60.0
_MAX_BACKOFF = 1800.0
_provision_backoff: dict[uuid.UUID, tuple[int, float]] = {}


def _record_provision_failure(row: ConfigManagerTenant) -> float:
    """Count a failed provision and return the seconds until the next attempt."""
    failures = _provision_backoff.get(row.id, (0, 0.0))[0] + 1
    delay = min(_MIN_BACKOFF * (2 ** (failures - 1)), _MAX_BACKOFF)
    _provision_backoff[row.id] = (failures, time.monotonic() + delay)
    logger.info(
        "config_manager_controller.provision_backoff",
        tenant_id=str(row.tenant_id),
        env_id=str(row.id),
        consecutive_failures=failures,
        retry_in_seconds=int(delay),
    )
    return delay


def _clear_provision_backoff(row: ConfigManagerTenant) -> None:
    _provision_backoff.pop(row.id, None)


def _provision_backoff_remaining(row: ConfigManagerTenant) -> float:
    """Seconds left before this environment may be provisioned again."""
    _failures, until = _provision_backoff.get(row.id, (0, 0.0))
    return max(0.0, until - time.monotonic())


class TenantSpec(BaseModel):
    target_cluster_tunnel_id: uuid.UUID | None = None
    base_hostname: str | None = None
    components: dict[str, bool] | None = None
    size_profile: str = "small"
    chart_version: str | None = None


class EnvironmentSpec(BaseModel):
    """Create body for a named environment.

    Unlike :class:`TenantSpec` there is no ``base_hostname`` knob: an
    environment's namespace and hostname are *derived* from its slug (see
    ``env_slug``) so the two can never disagree, and the slug itself is
    immutable because both are baked into live URLs.
    """

    name: str
    components: dict[str, bool] | None = None
    size_profile: str = "small"
    chart_version: str | None = None
    target_cluster_tunnel_id: uuid.UUID | None = None
    # Break-glass local admin for Nautobot — the one tool with its own login.
    # Optional: username falls back to "admin", password is generated when
    # omitted. Returned once in the create response and never again.
    admin_username: str | None = None
    admin_password: str | None = None


class TenantView(BaseModel):
    id: str
    tenant_id: str
    # Environment identity. Legacy single-environment rows were backfilled by
    # migration 0030, so these are always populated.
    name: str = ""
    slug: str = ""
    is_primary: bool = True
    state: str
    target_cluster_tunnel_id: str | None
    namespace: str
    base_hostname: str
    components: dict[str, Any]
    last_error: str | None
    urls: dict[str, Any] = {}
    # Version surface: what this tenant runs, what the image ships, and
    # whether the latter is newer — so callers don't re-implement semver.
    chart_version: str = ""
    latest_chart_version: str | None = None
    available_chart_versions: list[str] = []
    upgrade_available: bool = False
    # Break-glass Nautobot credential, populated ONLY on the create response
    # (the password is never re-served afterwards). See local_admin.
    admin_username: str | None = None
    admin_password: str | None = None


class UpgradeSpec(BaseModel):
    """Target version for an upgrade; omit to take the newest on disk."""

    chart_version: str | None = None


def _authenticate(authorization: str | None) -> ServiceTokenClaims:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = verify_service_token(authorization.split(None, 1)[1])
    except ServiceTokenError as e:
        logger.warning("config_manager_controller.auth_failed", error=str(e))
        raise HTTPException(status_code=401, detail="invalid token") from e
    if claims.purpose != _ACCEPTED_PURPOSE:
        raise HTTPException(status_code=403, detail="wrong token purpose")
    return claims


def _chart_path(chart_version: str) -> str:
    settings = get_settings()
    return os.path.join(
        settings.config_manager_chart_dir,
        f"nv-config-manager-{chart_version}",
    )


async def _resolve_target_tunnel(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    explicit: uuid.UUID | None,
) -> uuid.UUID | None:
    """Decide which cluster this tenant's NV-CM stack installs into.

    An explicit id is validated and honoured. Otherwise we auto-select the
    tenant's own connected cluster tunnel, so onboarding never has to remember
    to pass it — the historical failure mode was a NULL here silently meaning
    "install into the hub cluster", which then failed the Tier-A precheck
    because the hub carries none of NV-CM's singleton operators.

    Returns ``None`` only when the tenant genuinely has no connected tunnel;
    that preserves the documented hub-local mode instead of hard-failing.
    """
    if explicit is not None:
        ct = await db.get(ClusterTunnel, explicit)
        if ct is None:
            raise HTTPException(status_code=404, detail="cluster_tunnel not found")
        if ct.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="cluster_tunnel tenant mismatch")
        if ct.status != ClusterTunnelStatus.connected:
            raise HTTPException(
                status_code=409,
                detail=f"cluster_tunnel is {ct.status.value} — must be 'connected'",
            )
        return explicit

    rows = (
        await db.execute(
            select(ClusterTunnel)
            .where(
                ClusterTunnel.tenant_id == tenant_id,
                ClusterTunnel.status == ClusterTunnelStatus.connected,
            )
            .order_by(ClusterTunnel.last_handshake_at.desc().nullslast())
        )
    ).scalars().all()
    if not rows:
        logger.warning(
            "config_manager_controller.no_connected_tunnel",
            tenant_id=str(tenant_id),
        )
        return None
    chosen = rows[0]
    logger.info(
        "config_manager_controller.tunnel_auto_selected",
        tenant_id=str(tenant_id),
        cluster_tunnel_id=str(chosen.id),
        slug=chosen.slug,
    )
    return chosen.id


def _validate_chart_version(chart_version: str) -> None:
    """Reject a version with no vendored chart directory.

    ``_chart_path`` string-joins straight into a filesystem path, so an
    unvetted value from a request body would fail late and opaquely (or
    escape the chart dir). Validate at the API boundary instead.
    """
    settings = get_settings()
    known = chart_versions.available_versions(settings.config_manager_chart_dir)
    if chart_version not in known:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown chart_version {chart_version!r}; "
                f"this build ships: {', '.join(known) or '(none found)'}"
            ),
        )


def create_app() -> FastAPI:
    app = FastAPI(
        title="Daalu Config Manager Controller",
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
        task = getattr(app.state, "reconcile_task", None)
        if task is not None:
            task.cancel()

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
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="tenant not found")

        target_tunnel_id = await _resolve_target_tunnel(
            db, tenant_id, spec.target_cluster_tunnel_id
        )
        if spec.chart_version:
            _validate_chart_version(spec.chart_version)

        settings = get_settings()
        row = await _find_row(db, tenant_id)
        base_hostname = (
            spec.base_hostname
            or f"{tenant.slug}.{settings.cmtools_base_domain}"
        )
        if row is None:
            row = ConfigManagerTenant(
                tenant_id=tenant_id,
                state=ConfigManagerTenantState.pending,
                target_cluster_tunnel_id=target_tunnel_id,
                namespace=f"cm-{tenant.slug}",
                base_hostname=base_hostname,
                components=spec.components or dict(values_mod.DEFAULT_COMPONENTS),
                size_profile=spec.size_profile,
                chart_version=spec.chart_version
                or settings.config_manager_default_chart_version,
                # Per-tenant generated secrets (encrypted at rest).
                secrets_ciphertext=encrypt_secret(secrets.token_urlsafe(32)),
            )
            db.add(row)
        elif row.state == ConfigManagerTenantState.pending:
            row.target_cluster_tunnel_id = target_tunnel_id
            row.base_hostname = base_hostname
            if spec.components is not None:
                row.components = spec.components
            row.size_profile = spec.size_profile
            if spec.chart_version:
                row.chart_version = spec.chart_version
        await db.commit()
        await db.refresh(row)
        return _to_view(row)

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
            raise HTTPException(status_code=404, detail="no config_manager tenant")
        return _to_view(row)

    @app.get("/tenants/{tenant_id}/progress")
    async def tenant_progress(
        tenant_id: uuid.UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, Any]:
        """Live install progress: row state + pods + notable events.

        The UI polls this while the row is ``pending``/``provisioning`` so a
        multi-minute install is legible instead of a spinner — and so the two
        failure modes that actually bite (unschedulable pods, image pulls that
        can't verify TLS) are visible without kubectl.
        """
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        row = await _find_row(db, tenant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no config_manager tenant")
        return await _progress_payload(db, row)

    @app.post("/tenants/{tenant_id}/upgrade", response_model=TenantView)
    async def upgrade_tenant(
        tenant_id: uuid.UUID,
        spec: UpgradeSpec,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> TenantView:
        """Re-pin a tenant to a newer vendored chart and re-provision.

        Bumping ``chart_version`` alone is not enough: the reconciler
        short-circuits an ``active`` tenant whose release is already
        ``deployed`` (see ``_reconcile_one``), so the row is reset to
        ``pending`` to re-enter the provisioning path. This is deliberately
        non-blocking — the caller polls ``GET /tenants/{id}``.
        """
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        row = await _find_row(db, tenant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no config_manager tenant")
        return await _apply_upgrade(db, row, spec)

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
            raise HTTPException(status_code=404, detail="no config_manager tenant")
        row.state = ConfigManagerTenantState.deleting
        await db.commit()
        return {"status": "scheduled"}

    # ── Named environments ─────────────────────────────────────────────
    #
    # The plural surface. Same lifecycle as above, but each route addresses
    # one environment by id instead of implying the tenant's primary.

    @app.get("/tenants/{tenant_id}/environments", response_model=list[TenantView])
    async def list_environments(
        tenant_id: uuid.UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> list[TenantView]:
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        return [_to_view(r) for r in await _list_env_rows(db, tenant_id)]

    @app.post("/tenants/{tenant_id}/environments", response_model=TenantView)
    async def create_environment(
        tenant_id: uuid.UUID,
        spec: EnvironmentSpec,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> TenantView:
        """Create a new environment and queue its install.

        400 when the name can't be a slug, 409 when that slug is taken for
        this tenant — both carry a message written to be shown to the user,
        because the caller (the hub) surfaces the detail verbatim.
        """
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="tenant not found")

        try:
            slug = env_slug.slug_from_name(spec.name)
        except env_slug.InvalidEnvironmentName as e:
            # The exception text is authored for end users (see env_slug), so
            # pass it through rather than restating it.
            raise HTTPException(status_code=400, detail=str(e)) from e

        existing = await _list_env_rows(db, tenant_id)
        if any(r.slug == slug for r in existing):
            raise HTTPException(
                status_code=409,
                detail=f"an environment named {slug!r} already exists",
            )

        target_tunnel_id = await _resolve_target_tunnel(
            db, tenant_id, spec.target_cluster_tunnel_id
        )
        if spec.chart_version:
            _validate_chart_version(spec.chart_version)

        settings = get_settings()
        try:
            admin_user, admin_pw = local_admin.validate(
                spec.admin_username, spec.admin_password
            )
        except local_admin.LocalAdminError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        row = ConfigManagerTenant(
            tenant_id=tenant_id,
            name=spec.name.strip(),
            slug=slug,
            # The first environment a tenant creates becomes the primary, so
            # a tenant onboarded through this route is immediately resolvable
            # by every singular caller. Later ones are additive and must not
            # steal that role — the partial unique index would reject them
            # anyway, but failing here would be an opaque 500.
            is_primary=not existing,
            state=ConfigManagerTenantState.pending,
            target_cluster_tunnel_id=target_tunnel_id,
            namespace=env_slug.namespace_for(tenant.slug, slug),
            base_hostname=env_slug.base_hostname_for(
                tenant.slug, slug, settings.cmtools_base_domain
            ),
            components=spec.components or dict(values_mod.DEFAULT_COMPONENTS),
            size_profile=spec.size_profile,
            chart_version=spec.chart_version
            or settings.config_manager_default_chart_version,
            secrets_ciphertext=local_admin.to_ciphertext(admin_user, admin_pw),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        logger.info(
            "config_manager_controller.environment_created",
            tenant_id=str(tenant_id),
            env_id=str(row.id),
            slug=slug,
            is_primary=row.is_primary,
            # Never log the password — only whether the creator supplied one.
            admin_username=admin_user,
            admin_password_generated=not spec.admin_password,
        )
        view = _to_view(row)
        # The one and only time the password leaves the system. It stays
        # recoverable from secrets_ciphertext, but nothing re-serves it today.
        view.admin_username = admin_user
        view.admin_password = admin_pw
        return view

    @app.get(
        "/tenants/{tenant_id}/environments/{env_id}", response_model=TenantView
    )
    async def get_environment(
        tenant_id: uuid.UUID,
        env_id: uuid.UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> TenantView:
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        row = await _find_env_row(db, tenant_id, env_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such environment")
        return _to_view(row)

    @app.get("/tenants/{tenant_id}/environments/{env_id}/progress")
    async def environment_progress(
        tenant_id: uuid.UUID,
        env_id: uuid.UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, Any]:
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        row = await _find_env_row(db, tenant_id, env_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such environment")
        return await _progress_payload(db, row)

    @app.post(
        "/tenants/{tenant_id}/environments/{env_id}/upgrade",
        response_model=TenantView,
    )
    async def upgrade_environment(
        tenant_id: uuid.UUID,
        env_id: uuid.UUID,
        spec: UpgradeSpec,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> TenantView:
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        row = await _find_env_row(db, tenant_id, env_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such environment")
        return await _apply_upgrade(db, row, spec)

    @app.delete("/tenants/{tenant_id}/environments/{env_id}")
    async def destroy_environment(
        tenant_id: uuid.UUID,
        env_id: uuid.UUID,
        force: bool = False,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, str]:
        """Schedule teardown; the row is deleted once the cluster is clear.

        ``force`` drops the record without waiting for (or requiring) a
        successful uninstall. That is the escape hatch for an environment
        whose host cluster is gone or unreachable, where the teardown can
        never succeed and the row would otherwise be stuck in ``deleting``
        forever. It may leave cluster-side leftovers, so it is not the
        default.
        """
        claims = _authenticate(authorization)
        if claims.tenant_id != str(tenant_id):
            raise HTTPException(status_code=403, detail="tenant_id mismatch")
        row = await _find_env_row(db, tenant_id, env_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such environment")
        if force:
            await _forget_row(db, row, reason="force_deleted")
            return {"status": "deleted"}
        row.state = ConfigManagerTenantState.deleting
        await db.commit()
        return {"status": "scheduled"}

    return app


# ── Helpers ────────────────────────────────────────────────────────────


async def _find_row(
    db: AsyncSession, tenant_id: uuid.UUID
) -> ConfigManagerTenant | None:
    """The tenant's PRIMARY environment.

    Every singular ``/tenants/{id}…`` route resolves through here, so this one
    function is what keeps pre-environment callers working: "the tenant's
    config manager" now means "the environment marked primary". A partial
    unique index (migration 0030) guarantees at most one, so the
    ``scalar_one_or_none`` cannot raise on a consistent database.
    """
    return (
        await db.execute(
            select(ConfigManagerTenant).where(
                ConfigManagerTenant.tenant_id == tenant_id,
                ConfigManagerTenant.is_primary.is_(True),
            )
        )
    ).scalar_one_or_none()


async def _find_env_row(
    db: AsyncSession, tenant_id: uuid.UUID, env_id: uuid.UUID
) -> ConfigManagerTenant | None:
    """One environment by id, scoped to the tenant.

    The tenant predicate is not redundant with the primary-key lookup: it
    stops a caller holding a valid token for tenant A from addressing tenant
    B's environment by guessing its id.
    """
    return (
        await db.execute(
            select(ConfigManagerTenant).where(
                ConfigManagerTenant.id == env_id,
                ConfigManagerTenant.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()


async def _list_env_rows(
    db: AsyncSession, tenant_id: uuid.UUID
) -> list[ConfigManagerTenant]:
    return list(
        (
            await db.execute(
                select(ConfigManagerTenant)
                .where(ConfigManagerTenant.tenant_id == tenant_id)
                # Primary first, then stable by slug — the UI renders this
                # order directly and a reshuffling list reads as churn.
                .order_by(
                    ConfigManagerTenant.is_primary.desc(),
                    ConfigManagerTenant.slug,
                )
            )
        ).scalars().all()
    )


async def _progress_payload(
    db: AsyncSession, row: ConfigManagerTenant
) -> dict[str, Any]:
    """Live install progress for one environment row.

    Shared by the singular and the per-environment progress routes so the two
    can never report differently-shaped state.
    """
    kubeconfig = await _kubeconfig_for(db, row)
    detail = await progress.namespace_progress(row.namespace, kubeconfig)
    # The in-cluster gateway address the hub tool proxy dials. Read back from
    # the cluster (compute_urls can't derive it — it's a Service address, not
    # a public URL); None until Envoy Gateway has provisioned the proxy
    # Service.
    gateway_url = await progress.gateway_internal_url(row.namespace, kubeconfig)
    await _stamp_gateway_url(db, row, gateway_url)
    nautobot_token_ct = await _stamp_nautobot_token(db, row, kubeconfig)
    return {
        "id": str(row.id),
        "name": row.name or "",
        "slug": row.slug or "",
        # The hub writes its single Integration row only for the primary, so
        # it has to be able to tell from a progress poll alone.
        "is_primary": bool(row.is_primary),
        "state": row.state.value,
        "namespace": row.namespace,
        "last_error": row.last_error,
        "chart_version": row.chart_version or "",
        "urls": row.urls or {},
        # Enough for the hub to write its Integration row from a poll,
        # without a second round-trip to GET /tenants/{id}.
        "base_hostname": row.base_hostname,
        "gateway_internal_url": gateway_url,
        # Ciphertext (hub and controller share the crypto key): lets the hub
        # copy the bundled-Nautobot credential onto its Integration row from a
        # progress poll, the same way gateway_internal_url travels.
        "nautobot_token_ciphertext": nautobot_token_ct,
        "components": row.components or {},
        "target_cluster_tunnel_id": (
            str(row.target_cluster_tunnel_id)
            if row.target_cluster_tunnel_id
            else None
        ),
        **detail,
    }


async def _stamp_gateway_url(
    db: AsyncSession, row: ConfigManagerTenant, gateway_url: str | None
) -> None:
    """Persist the resolved in-cluster gateway address onto the row.

    The hub tool proxy historically read this out of the tenant's
    ``config_manager`` Integration config — but there is exactly one such row
    per tenant and it tracks the primary environment only, so a non-primary
    environment would have nowhere to publish its gateway and would be
    unreachable. Keeping it on the row makes every environment self-describing
    (the hub reads the same database), and the Integration copy stays as-is
    for the primary so nothing downstream has to change at once.

    Only writes on an actual change, and never blanks a known-good value with
    the ``None`` a mid-install poll returns.
    """
    if not gateway_url or row.gateway_internal_url == gateway_url:
        return
    row.gateway_internal_url = gateway_url
    await db.commit()


async def _stamp_nautobot_token(
    db: AsyncSession,
    row: ConfigManagerTenant,
    kubeconfig: dict[str, Any] | None,
) -> str | None:
    """Mirror the env's Nautobot API token onto the row; return its ciphertext.

    The vendored Deployer generates a superuser API token into the namespace's
    ``nautobot-token`` Secret. The hub's SoT client / bulk import authenticate
    with it, so it's persisted (encrypted, in the row's secrets bag) the first
    time a progress poll can read it, and only re-read from the cluster when
    missing. Best-effort like :func:`_stamp_gateway_url` — ``None`` mid-install.
    """
    existing = local_admin.api_token_from_row(row)
    if existing:
        return encrypt_secret(existing)
    token = await progress.nautobot_api_token(row.namespace, kubeconfig)
    if not token:
        return None
    row.secrets_ciphertext = local_admin.with_api_token(row, token)
    await db.commit()
    return encrypt_secret(token)


async def _apply_upgrade(
    db: AsyncSession, row: ConfigManagerTenant, spec: UpgradeSpec
) -> TenantView:
    """Re-pin one environment to a newer vendored chart and re-provision.

    Bumping ``chart_version`` alone is not enough: the reconciler
    short-circuits an ``active`` row whose release is already ``deployed``
    (see ``_reconcile_one``), so the row is reset to ``pending`` to re-enter
    the provisioning path. Deliberately non-blocking — the caller polls.
    """
    settings = get_settings()
    target = spec.chart_version or chart_versions.latest_version(
        settings.config_manager_chart_dir
    )
    if not target:
        raise HTTPException(
            status_code=409, detail="no vendored charts found in this build"
        )
    _validate_chart_version(target)

    if row.state in (
        ConfigManagerTenantState.deleting,
        ConfigManagerTenantState.destroyed,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"tenant is {row.state.value} — cannot upgrade",
        )
    if not chart_versions.is_upgrade(row.chart_version, target):
        raise HTTPException(
            status_code=409,
            detail=f"{target} is not newer than the current {row.chart_version!r}",
        )

    # Re-resolve the tunnel: an upgrade is the natural point to correct a
    # row that predates auto-selection and still points at the hub.
    row.target_cluster_tunnel_id = await _resolve_target_tunnel(
        db, row.tenant_id, row.target_cluster_tunnel_id
    )
    row.chart_version = target
    row.state = ConfigManagerTenantState.pending
    row.last_error = None
    _clear_provision_backoff(row)
    await db.commit()
    await db.refresh(row)
    logger.info(
        "config_manager_controller.upgrade_scheduled",
        tenant_id=str(row.tenant_id),
        env_id=str(row.id),
        chart_version=target,
    )
    return _to_view(row)


def _to_view(row: ConfigManagerTenant) -> TenantView:
    chart_dir = get_settings().config_manager_chart_dir
    available = chart_versions.available_versions(chart_dir)
    latest = available[-1] if available else None
    return TenantView(
        chart_version=row.chart_version or "",
        latest_chart_version=latest,
        available_chart_versions=available,
        upgrade_available=bool(
            latest and chart_versions.is_upgrade(row.chart_version or "", latest)
        ),
        id=str(row.id),
        tenant_id=str(row.tenant_id),
        name=row.name or "",
        slug=row.slug or "",
        is_primary=bool(row.is_primary),
        state=row.state.value,
        target_cluster_tunnel_id=(
            str(row.target_cluster_tunnel_id)
            if row.target_cluster_tunnel_id
            else None
        ),
        namespace=row.namespace,
        base_hostname=row.base_hostname,
        components=row.components or {},
        last_error=row.last_error,
        urls=row.urls or {},
    )


# ── Reconcile loop ─────────────────────────────────────────────────────


def _teardown_first(
    rows: list[ConfigManagerTenant],
) -> list[ConfigManagerTenant]:
    """Order a tick's rows so teardowns are reconciled before provisions.

    A tick is only as fast as the work in it, and a provision holds this
    sequential loop for a full multi-minute Deployer pass. A delete queued
    behind two of those waits out the whole tick before anything happens to
    it. It is also the order that unblocks the rest: an uninstall frees the
    cluster capacity a failing install may be exactly the size of.
    """
    return sorted(
        rows,
        key=lambda r: r.state
        not in (
            ConfigManagerTenantState.deleting,
            ConfigManagerTenantState.destroyed,
        ),
    )


async def _reconcile_loop() -> None:
    from daalu_automation.database import AsyncSessionLocal

    logger.info("config_manager_controller.reconcile_loop_started")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                # Every row, `destroyed` included: a tombstone left by an older
                # build is swept away here rather than lingering as a ghost
                # tile in the UI (and holding its slug against a re-create).
                rows = (
                    await db.execute(select(ConfigManagerTenant))
                ).scalars().all()
                for row in _teardown_first(rows):
                    try:
                        await _reconcile_one(db, row)
                    except Exception as e:  # noqa: BLE001
                        logger.exception(
                            "config_manager_controller.reconcile_row_failed",
                            tenant_id=str(row.tenant_id),
                        )
                        # Best-effort: the row may have been deleted while this
                        # iteration ran, in which case there is nothing to
                        # stamp and the rollback keeps the session usable.
                        try:
                            row.last_error = str(e)[:1000]
                            await db.commit()
                        except Exception:  # noqa: BLE001
                            await db.rollback()
                            logger.warning(
                                "config_manager_controller.error_stamp_failed",
                                tenant_id=str(row.tenant_id),
                            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("config_manager_controller.reconcile_iteration_failed")
        await asyncio.sleep(_RECONCILE_INTERVAL)


async def _kubeconfig_for(
    db: AsyncSession, row: ConfigManagerTenant
) -> dict[str, Any] | None:
    if row.target_cluster_tunnel_id is None:
        return None  # local/in-cluster: helm uses its default KUBECONFIG
    return await _load_customer_kubeconfig(db, row.target_cluster_tunnel_id)


def _runner_for(row: ConfigManagerTenant) -> HelmRunner:
    return HelmRunner(chart_path=_chart_path(row.chart_version))


def _deployer_runner_for(row: ConfigManagerTenant) -> DeployerRunner:
    return DeployerRunner(chart_dir=_chart_path(row.chart_version))


async def _provision(
    row: ConfigManagerTenant, kubeconfig: dict[str, Any] | None, settings: Any
) -> tuple[bool, str]:
    """Install/upgrade the release; returns ``(ok, error_text)``.

    Uses the vendored NV-CM Deployer when ``config_manager_use_deployer`` is
    set (the path that breaks the secret-assembler deadlock), else the legacy
    bare-helm HelmRunner. Both are normalised to ``(ok, error)`` so the state
    machine below doesn't care which ran.
    """
    if settings.config_manager_use_deployer:
        res = await _deployer_runner_for(row).upgrade_install(
            row=row, kubeconfig=kubeconfig
        )
        return res.ok, res.error
    vals = values_mod.render_values(
        row, harbor_registry=settings.config_manager_harbor_registry or None
    )
    helm_res = await _runner_for(row).upgrade_install(
        release=f"cm-{row.tenant_id.hex[:12]}",
        namespace=row.namespace,
        values=vals,
        kubeconfig=kubeconfig,
        wait=True,
    )
    return helm_res.ok, helm_res.stderr


async def _teardown(
    row: ConfigManagerTenant, kubeconfig: dict[str, Any] | None, settings: Any
) -> tuple[bool, str]:
    """Uninstall the release; returns ``(ok, error_text)``."""
    if settings.config_manager_use_deployer:
        res = await _deployer_runner_for(row).uninstall(row=row, kubeconfig=kubeconfig)
        return res.ok, res.error
    helm_res = await _runner_for(row).uninstall(
        release=f"cm-{row.tenant_id.hex[:12]}",
        namespace=row.namespace,
        kubeconfig=kubeconfig,
    )
    return helm_res.ok, helm_res.stderr


async def _forget_row(
    db: AsyncSession, row: ConfigManagerTenant, *, reason: str
) -> None:
    """Delete the environment record, logging what it was before it went.

    The row carries the encrypted per-tenant secrets bag (chart secrets, the
    Nautobot break-glass credential and its API token), so dropping it is also
    what retires those — there is no cluster left for them to unlock.
    """
    tenant_id, env_id, slug = str(row.tenant_id), str(row.id), row.slug or ""
    _clear_provision_backoff(row)
    await db.delete(row)
    await db.commit()
    logger.info(
        "config_manager_controller.environment_forgotten",
        reason=reason,
        tenant_id=tenant_id,
        env_id=env_id,
        slug=slug,
    )


async def _current_state_in_db(
    db: AsyncSession, row: ConfigManagerTenant
) -> ConfigManagerTenantState | None:
    """Re-read this row's state from the database, ignoring the identity map.

    A provision holds ``row`` in memory for as long as the Helm rollout takes
    (up to 15 minutes), so anything that changed the row meanwhile — a
    scheduled delete, or the row being removed outright — is invisible to the
    in-memory copy. Returns ``None`` if the row no longer exists.
    """
    return (
        await db.execute(
            select(ConfigManagerTenant.state).where(
                ConfigManagerTenant.id == row.id
            )
        )
    ).scalar_one_or_none()


async def _reconcile_one(db: AsyncSession, row: ConfigManagerTenant) -> None:
    settings = get_settings()

    # `row` was loaded at the top of this tick, but a tick runs for as long as
    # the work in it: every provision blocks this sequential loop for a full
    # Deployer pass. So by the time a row's turn comes its in-memory state can
    # be minutes stale — and acting on it *writes that stale state back*. That
    # is how a scheduled delete was lost: DELETE set `deleting`, the loop
    # reached the row still believing it was `error`, and stamped
    # `provisioning` over it. The delete was gone, the environment reinstalled
    # itself, and the user saw error→provisioning→error forever. Re-read the
    # authoritative state before acting on it. (The check after `_provision`
    # below closes the other half of the same window.)
    committed_state = await _current_state_in_db(db, row)
    if committed_state is None:
        # Deleted underneath us — nothing to reconcile, and the session must
        # not keep holding the orphan.
        db.expunge(row)
        return
    if committed_state != row.state:
        # `expire_on_commit=False` means a re-select hands back this same
        # cached object untouched, so refresh explicitly.
        await db.refresh(row)

    if row.state == ConfigManagerTenantState.destroyed:
        # A tombstone from an older build, which used to park the row here
        # forever. Teardown already succeeded for it, so there is nothing left
        # to uninstall — drop the record so the environment stops existing.
        await _forget_row(db, row, reason="tombstone_swept")
        return

    if row.state == ConfigManagerTenantState.deleting:
        kubeconfig = await _kubeconfig_for(db, row)
        ok, err = await _teardown(row, kubeconfig, settings)
        if ok:
            # Delete the row outright rather than parking it in `destroyed`.
            # A retained row is indistinguishable from a live environment to
            # every reader (the UI lists it, the slug stays taken, and
            # `_find_row` hands the primary back to the next provision), so
            # "deleted" has to mean gone.
            await _forget_row(db, row, reason="tenant_destroyed")
        else:
            row.last_error = err[:1000]
            await db.commit()
        return

    if row.state in (
        ConfigManagerTenantState.pending,
        ConfigManagerTenantState.error,
        ConfigManagerTenantState.provisioning,
        ConfigManagerTenantState.active,
    ):
        # A row that has just failed is left alone until its backoff expires.
        # Only `error` is gated: `pending` means somebody asked for this
        # install explicitly, and the other two are not failure states.
        if row.state == ConfigManagerTenantState.error:
            wait = _provision_backoff_remaining(row)
            if wait > 0:
                logger.debug(
                    "config_manager_controller.provision_deferred",
                    tenant_id=str(row.tenant_id),
                    env_id=str(row.id),
                    retry_in_seconds=int(wait),
                )
                return

        kubeconfig = await _kubeconfig_for(db, row)

        # Steady-state short-circuit: an already-`active` tenant whose helm
        # release is `deployed` needs no re-`helm upgrade`. The reconcile loop
        # runs every 30s; re-running `helm upgrade --wait` each tick mints a
        # fresh revision (the chart values aren't bit-stable), churning the row
        # active↔provisioning and risking a `pending-upgrade` lock if an
        # upgrade is interrupted (e.g. a controller image roll). k8s already
        # self-heals the workloads, so skip — just refresh the readiness stamp.
        # To roll out a chart/spec change, reset the row to `pending` (that
        # path still re-provisions). Any uncertainty (helm error, not-deployed)
        # falls through to a full provision.
        if (
            row.state == ConfigManagerTenantState.active
            and settings.config_manager_use_deployer
            and await _deployer_runner_for(row).release_status(
                row=row, kubeconfig=kubeconfig
            )
            == "deployed"
        ):
            row.last_ready_at = datetime.now(tz=timezone.utc)
            await db.commit()
            # Telemetry collectors are env infrastructure the controller owns
            # (see collectors.py) — ensure them on the steady-state path too,
            # so a rebuilt cluster heals without a full re-provision. Cached
            # by content hash; best-effort by construction.
            await collectors.ensure_collectors(db, row, kubeconfig)
            return

        if row.state in (
            ConfigManagerTenantState.pending,
            ConfigManagerTenantState.error,
        ):
            row.state = ConfigManagerTenantState.provisioning
            await db.commit()

        # Tier-A precheck: refuse to install into a host cluster missing
        # the shared GatewayClass / cert-manager / CNPG. Surfaces a precise
        # "missing …" error instead of an opaque helm failure (§64.2/§64.6).
        if not settings.config_manager_skip_host_precheck:
            ready, missing = await prechecks.host_cluster_ready(kubeconfig)
            if not ready:
                row.state = ConfigManagerTenantState.error
                row.last_error = (
                    "host cluster not ready (Tier-A singletons missing): "
                    + ", ".join(missing)
                )[:1000]
                _record_provision_failure(row)
                await db.commit()
                logger.warning(
                    "config_manager_controller.host_not_ready",
                    tenant_id=str(row.tenant_id),
                    missing=missing,
                )
                return

        # A release left mid-operation by a controller restart blocks every
        # subsequent install with "another operation is in progress", which the
        # retry loop can never clear on its own. Unwedge before trying.
        if settings.config_manager_use_deployer:
            try:
                await _deployer_runner_for(row).clear_pending_release(
                    row=row, kubeconfig=kubeconfig
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "config_manager_controller.unwedge_check_failed",
                    tenant_id=str(row.tenant_id),
                )

        try:
            ok, err = await _provision(row, kubeconfig, settings)
        except Exception as e:  # noqa: BLE001
            # A raising runner (as opposed to one returning ok=False) used to
            # escape to the loop-level handler, which records last_error but
            # leaves state at `provisioning` — a combination the state machine
            # can never clear, so the row retried a failing install forever.
            logger.exception(
                "config_manager_controller.provision_raised",
                tenant_id=str(row.tenant_id),
            )
            ok, err = False, str(e)

        # The rollout above can run for many minutes. Re-read the row before
        # committing: a delete scheduled meanwhile must not be overwritten by
        # this now-stale in-memory state, and the row may be gone entirely.
        latest_state = await _current_state_in_db(db, row)
        if latest_state is None or latest_state in (
            ConfigManagerTenantState.deleting,
            ConfigManagerTenantState.destroyed,
        ):
            logger.info(
                "config_manager_controller.reconcile_superseded",
                tenant_id=str(row.tenant_id),
                latest_state=latest_state.value if latest_state else "(deleted)",
            )
            db.expunge(row)
            return

        if ok:
            row.urls = values_mod.compute_urls(row.base_hostname)
            row.last_ready_at = datetime.now(tz=timezone.utc)
            row.last_error = None
            row.state = ConfigManagerTenantState.active
            _clear_provision_backoff(row)
            await db.commit()
            # Resolve and persist the gateway address here, not only from a
            # progress poll: a non-primary environment has no Integration row
            # to fall back on, so if nobody happened to poll it the tool proxy
            # would have no way to reach it. Best-effort — a missing address
            # is retried on the next tick.
            try:
                await _stamp_gateway_url(
                    db,
                    row,
                    await progress.gateway_internal_url(row.namespace, kubeconfig),
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "config_manager_controller.gateway_url_probe_failed",
                    tenant_id=str(row.tenant_id),
                    env_id=str(row.id),
                )
            logger.info(
                "config_manager_controller.tenant_active",
                tenant_id=str(row.tenant_id),
            )
        else:
            row.state = ConfigManagerTenantState.error
            row.last_error = err[:1000]
            _record_provision_failure(row)
            await db.commit()
