from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_admin, current_tenant_id
from daalu_automation.api.schemas import IntegrationDescriptorOut
from daalu_automation.config import get_settings
from daalu_automation.core.integrations import (
    get_integration,
    list_integrations,
)
from daalu_automation.database import get_db
from daalu_automation.models import (  # noqa: F401
    ClusterTunnel,
    Integration,
    IntegrationStatus,
    User,
)

router = APIRouter(prefix="/integrations", tags=["integrations"])

# Providers a tenant may connect more than once — each row is a distinct
# named instance. Today only Kubernetes (one row per cluster, keyed by
# ``name``); every other provider stays single-row per (tenant, provider).
_MULTI_INSTANCE_PROVIDERS = {"kubernetes"}


# Provider → which config fields are sensitive and should never be
# echoed back on GET. The DB row is the source of truth; the API
# masks these fields with a placeholder so the UI knows the field is
# set without exposing the value.
_REDACT_FIELDS = {
    "smtp": {"password"},
    "slack": {"webhook_url"},
    "pagerduty": {"api_token"},
    "loki": {"password"},
    "opensearch": {"password"},
    "grafana": {"api_token", "token", "bearer_token", "password"},
    "kubernetes": {"kubeconfig"},
    "aws": {"secret_access_key", "session_token"},
    "gcp": {"service_account_json"},
    "azure": {"client_secret"},
    # Both the ciphertext shape (from operator PUTs / hosted provisioning)
    # and the plaintext shape (from the onboarding wizard's BYO form) are
    # redacted on GET so the wizard pre-fill never echoes a real secret.
    "nautobot": {
        "token",
        "token_ciphertext",
        "webhook_secret",
        "webhook_secret_ciphertext",
    },
    "ssh_credentials": {
        "password",
        "private_key",
        "private_key_ciphertext",
        "password_ciphertext",
    },
    "redfish_credentials": {"password", "password_ciphertext"},
    "network_credentials": {
        "password",
        "private_key",
        "enable_password",
        "password_ciphertext",
        "private_key_ciphertext",
        "enable_password_ciphertext",
    },
    # NV-CM connection: the hub's Keycloak service-client secret + the
    # bundled-Nautobot token (both encrypted) must never echo back to the UI.
    "config_manager": {
        "keycloak_client_secret_ciphertext",
        "nautobot_token_ciphertext",
        "nautobot_token",
    },
    # Tinkerbell mgmt-cluster access: the inline kubeconfig is sensitive.
    "tinkerbell": {"kubeconfig"},
}


def _redact(provider: str, cfg: dict) -> dict:
    redact = _REDACT_FIELDS.get(provider, set())
    return {
        k: ("***" if k in redact and v else v) for k, v in cfg.items()
    }


# Browser forms submit credential secrets in PLAINTEXT keys (they can't
# encrypt client-side); the resolvers (``change_proposals.resolve_credentials``)
# read ONLY the ``*_ciphertext`` shape. Seal the plaintext on write so a
# UI-saved credential is actually usable — storing it verbatim leaves the
# secret unencrypted at rest *and* invisible to the resolver, which
# surfaces as "no SSH credentials to probe devices" on the topology page.
_SEAL_ON_WRITE: dict[str, dict[str, str]] = {
    "ssh_credentials": {
        "password": "password_ciphertext",
        "private_key": "private_key_ciphertext",
    },
    "redfish_credentials": {"password": "password_ciphertext"},
    "network_credentials": {
        "password": "password_ciphertext",
        "private_key": "private_key_ciphertext",
        "enable_password": "enable_password_ciphertext",
    },
}


def _seal_credential_config(provider: str, cfg: dict) -> dict:
    mapping = _SEAL_ON_WRITE.get(provider)
    if not mapping:
        return cfg
    from daalu_automation.core.crypto import encrypt_secret

    out = dict(cfg)
    for plain, ct_key in mapping.items():
        secret = out.pop(plain, None)
        if secret:
            out[ct_key] = encrypt_secret(secret)
    return out


@router.get("", response_model=list[IntegrationDescriptorOut])
async def list_integrations_route():
    """Enumerate adapter descriptors.

    "configured" here reflects env-default presence — the page renders
    "needs setup" before any tenant has wired the provider. Per-tenant
    configured-state lands alongside the tenant integration-config CRUD.
    """
    settings = get_settings()
    settings_dict = settings.model_dump()
    out = []
    for d in list_integrations():
        configured = all(
            bool(settings_dict.get(key.lower(), "")) for key in d.required_settings
        )
        out.append(
            IntegrationDescriptorOut(
                provider=d.provider,
                module=d.module,
                display_name=d.display_name,
                description=d.description,
                required_settings=list(d.required_settings),
                configured=configured,
            )
        )
    return out


@router.post("/{provider}/ingest")
async def trigger_ingest(
    provider: str,
    tenant_id=Depends(current_tenant_id),
):
    """Trigger an on-demand ingest for the caller's tenant.

    Cross-tenant ingestion (a superuser firing ingest for another
    tenant) is intentionally not surfaced here — that path goes through
    a tenant-targeted superuser endpoint, so a tenant-admin can't
    accidentally kick off another tenant's adapter run.
    """
    try:
        adapter = get_integration(provider)
    except KeyError as e:
        raise HTTPException(404, f"unknown provider: {provider}") from e
    emitted = await adapter.ingest(tenant_id)
    return {"events_emitted": emitted}


# ---------------------------------------------------------------------------
# Per-tenant integration configuration — replaces .env for Phase-2 onboarding
# ---------------------------------------------------------------------------


class IntegrationConfigIn(BaseModel):
    config: dict
    name: str | None = None
    # When set, dial the integration's URL through this cluster's
    # daalu-edge HTTP forward proxy (see core/cluster_proxy.py). Used by
    # observability integrations whose URL only resolves inside a
    # federated workload cluster. Pass an explicit `null` to detach an
    # integration from its cluster on PUT; omit the field entirely to
    # leave the existing value unchanged.
    cluster_tunnel_id: uuid.UUID | None = None
    # Identifies which row to update for providers that allow several
    # instances (e.g. multiple `kubernetes` clusters). Omit on PUT to
    # create a new instance (multi-instance providers) or to upsert the
    # single row (everything else).
    cluster_id: uuid.UUID | None = None


class IntegrationConfigOut(BaseModel):
    id: str
    provider: str
    module: str
    name: str
    status: str
    config: dict
    cluster_tunnel_id: str | None = None
    # Health-probe metadata written by the integrations.health_check
    # beat task. ``last_probed_at`` is null for rows created since the
    # last tick (or while beat was down); ``last_error`` carries the
    # adapter's failure reason when status='error'.
    last_probed_at: str | None = None
    last_error: str | None = None


def _to_config_out(row: Integration) -> IntegrationConfigOut:
    return IntegrationConfigOut(
        id=str(row.id),
        provider=row.provider,
        module=row.module,
        name=row.name,
        status=row.status.value,
        config=_redact(row.provider, row.config or {}),
        cluster_tunnel_id=str(row.cluster_tunnel_id) if row.cluster_tunnel_id else None,
        last_probed_at=row.last_probed_at.isoformat() if row.last_probed_at else None,
        last_error=row.last_error,
    )


@router.get("/config", response_model=list[IntegrationConfigOut])
async def list_tenant_configs(
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    """List the caller's tenant's integration-config rows.

    Sensitive values (webhook URL, SMTP password, API tokens) are
    redacted in the response — they're verified by sending, never
    echoed back. The DB row remains the source of truth.
    """
    rows = (
        await db.execute(
            select(Integration).where(Integration.tenant_id == tenant_id)
        )
    ).scalars().all()
    return [_to_config_out(r) for r in rows]


@router.put("/config/{provider}", response_model=IntegrationConfigOut)
async def upsert_tenant_config(
    provider: str,
    payload: IntegrationConfigIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Create or update the tenant's config for one provider.

    Requires is_admin on the calling user (a tenant-admin manages their
    own tenant's integrations). The full ``config`` dict replaces what
    was there — partial updates are intentionally not supported so a
    half-rotated credential never sits in a mixed state.
    """
    # Adapter providers (prometheus, pagerduty, synthetic-infra) come
    # from the registry. Adapter-less providers
    # — Slack/SMTP for notify, the log/metric stacks for tool-call
    # reads, and the cloud accounts (aws/gcp/azure) for the
    # alert-chat agent's cloud tools — don't have an ingest adapter;
    # they only carry configuration that other code consumes. Both
    # shapes are valid rows on the integrations table.
    _ADAPTERLESS = {
        "slack": ("infra", "Slack"),
        "smtp": ("infra", "Email (SMTP)"),
        "loki": ("infra", "Loki (logs)"),
        "thanos": ("infra", "Thanos (long-history metrics)"),
        "opensearch": ("infra", "OpenSearch (logs)"),
        "grafana": ("infra", "Grafana (dashboards)"),
        "kubernetes": ("infra", "Kubernetes (managed cluster)"),
        "aws": ("infra", "AWS account"),
        "gcp": ("infra", "Google Cloud project"),
        "azure": ("infra", "Azure subscription"),
        "nautobot": ("infra", "Nautobot (Source of Truth)"),
        "ssh_credentials": ("infra", "SSH credentials (managed Linux servers)"),
        "redfish_credentials": ("infra", "Redfish credentials (server BMCs)"),
        "network_credentials": (
            "infra",
            "Network device credentials (SSH / NETCONF, Junos / IOS-XR / EOS)",
        ),
        # Network config-management plane (NV-CM) — service URLs + the
        # Keycloak service client the hub uses against svc-* endpoints.
        "config_manager": ("infra", "NVIDIA Config Manager (network fabric)"),
        # Server lifecycle plane — the Tinkerbell mgmt cluster the hub
        # applies provisioning CRs to (kubeconfig / cluster-tunnel ref).
        "tinkerbell": ("infra", "Tinkerbell (bare-metal servers)"),
    }
    try:
        descriptor = get_integration(provider).descriptor
        module = descriptor.module
        display = descriptor.display_name
    except KeyError:
        if provider not in _ADAPTERLESS:
            raise HTTPException(404, f"unknown provider: {provider}") from None
        module, display = _ADAPTERLESS[provider]

    # Pick the row to update (or decide to create one). Multi-instance
    # providers (kubernetes) key on cluster_id when editing, or on name when
    # re-connecting an existing cluster; with neither we create a new row.
    multi = provider in _MULTI_INSTANCE_PROVIDERS
    existing = None
    if payload.cluster_id is not None:
        existing = (
            await db.execute(
                select(Integration).where(
                    Integration.id == payload.cluster_id,
                    Integration.tenant_id == user.tenant_id,
                    Integration.provider == provider,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise HTTPException(404, "integration instance not found")
    elif multi:
        if payload.name:
            existing = (
                await db.execute(
                    select(Integration).where(
                        Integration.tenant_id == user.tenant_id,
                        Integration.provider == provider,
                        Integration.name == payload.name,
                    )
                )
            ).scalars().first()
        # else: leave existing=None → create a fresh instance
    else:
        existing = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == user.tenant_id,
                    Integration.provider == provider,
                )
            )
        ).scalar_one_or_none()

    # cluster_tunnel_id semantics:
    # - field present and a UUID → attach/move to that cluster (validated
    #   below: must belong to this tenant)
    # - field present and explicitly null → detach
    # - field absent (not in request body) → leave whatever's stored
    set_cluster = "cluster_tunnel_id" in payload.model_fields_set
    if set_cluster and payload.cluster_tunnel_id is not None:
        # Reject cross-tenant attachment outright — silently dialing
        # another tenant's tunnel is the kind of bug we never want to ship.
        ct = await db.get(ClusterTunnel, payload.cluster_tunnel_id)
        if ct is None or ct.tenant_id != user.tenant_id:
            raise HTTPException(
                400, f"cluster_tunnel_id {payload.cluster_tunnel_id} not found for this tenant"
            )

    config = _seal_credential_config(provider, payload.config)
    if existing is None:
        row = Integration(
            tenant_id=user.tenant_id,
            provider=provider,
            module=module,
            name=payload.name or display,
            status=IntegrationStatus.connected,
            config=config,
            cluster_tunnel_id=payload.cluster_tunnel_id if set_cluster else None,
        )
        db.add(row)
    else:
        existing.config = config
        existing.status = IntegrationStatus.connected
        if payload.name:
            existing.name = payload.name
        if set_cluster:
            existing.cluster_tunnel_id = payload.cluster_tunnel_id
        row = existing
    await db.commit()
    await db.refresh(row)
    return _to_config_out(row)


@router.delete("/config/{provider}", status_code=204)
async def delete_tenant_config(
    provider: str,
    cluster_id: uuid.UUID | None = Query(
        None,
        description=(
            "Which instance to delete for multi-instance providers "
            "(kubernetes). Required when more than one row exists."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    stmt = select(Integration).where(
        Integration.tenant_id == user.tenant_id,
        Integration.provider == provider,
    )
    if cluster_id is not None:
        stmt = stmt.where(Integration.id == cluster_id)
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        raise HTTPException(404, "integration not configured")
    if len(rows) > 1 and cluster_id is None:
        # Ambiguous: a multi-instance provider with several rows and no
        # selector. Refuse rather than delete an arbitrary one.
        raise HTTPException(
            400,
            "multiple instances configured — pass ?cluster_id=<id> to pick one",
        )
    await db.delete(rows[0])
    await db.commit()
